# -*- coding: utf-8 -*-
# Copyright 2021 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License")
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import datetime
import json
import logging
import os
import re
from typing import Any, NoReturn, Optional, Type, TypeVar

import flask
import flask.views
import werkzeug.exceptions

import google.appengine.api
from google.cloud import ndb  # type: ignore

import settings
from api import api_specs
from framework import csp
from framework import permissions
from framework import secrets
from framework import users
from framework import utils
from framework import xsrf
from internals import approval_defs
from internals import notifier_helpers
from internals import user_models
from internals.core_enums import (
  ALL_ORIGIN_TRIAL_STAGE_TYPES,
  OT_ACTIVATION_FAILED,
  OT_CREATION_FAILED,
  OT_READY_FOR_CREATION)
from internals.core_models import FeatureEntry, MilestoneSet, Stage
from internals.data_types import CHANGED_FIELDS_LIST_TYPE

from flask import session
from flask import render_template
from flask_cors import CORS
from gen.py.chromestatus_openapi.chromestatus_openapi.models.base_model import Model

# Our API responses are prefixed with this ro prevent attacks that
# exploit <script src="...">.  See go/xssi.
XSSI_PREFIX = ')]}\'\n'


# See https://www.regextester.com/93901 for url regex
SCHEME_PATTERN = r'((?P<scheme>[a-z]+):(\/\/)?)?'
DOMAIN_PATTERN = r'([\w-]+(\.[\w-]+)+)'
PATH_PARAMS_ANCHOR_PATTERN = r'([\w.,@?^=%&:/~+#-]*[\w@?^=%&/~+#-])?'
URL_RE = re.compile(r'\b%s%s%s\b' % (
    SCHEME_PATTERN, DOMAIN_PATTERN, PATH_PARAMS_ANCHOR_PATTERN))
ALLOWED_SCHEMES = [None, 'http', 'https']

# Generic type variable for our model entities.
M = TypeVar('M', bound=ndb.Model)

class BaseHandler(flask.views.MethodView):

  @property
  def request(self):
    return flask.request

  def abort(self, status, msg=None, **kwargs) -> NoReturn:
    """Support webapp2-style, e.g., self.abort(400)."""
    if msg:
      if status == 500:
        logging.error('ISE: %s' % msg)
      else:
        logging.info('Abort %r: %s' % (status, msg))
      flask.abort(status, description=msg, **kwargs)
    else:
      logging.info('Abort %r' % status)
      flask.abort(status, **kwargs)

  def redirect(self, url):
    """Support webapp2-style, e.g., return self.redirect(url)."""
    return flask.redirect(url)

  def get_current_user(self, required=False):
    # TODO(jrobbins): oauth support
    current_user = users.get_current_user()

    if required and not current_user:
      self.abort(403, msg='User must be signed in')
    return current_user

  def get_json_param_dict(self) -> dict:
    """Return the JSON content in the body of the request."""
    return self.request.get_json(force=True, silent=True) or {}

  def get_param(
      self, name, default=None, required=True, validator=None, allowed=None):
    """Get the specified JSON parameter."""
    json_body = self.request.get_json(force=True, silent=True) or {}
    val = json_body.get(name, default)
    if required and val is None:
      self.abort(400, msg='Missing parameter %r' % name)
    if val and validator and not validator(val):
      self.abort(400, msg='Invalid value for parameter %r' % name)
    if val and allowed and val not in allowed:
      self.abort(400, msg='Unexpected value for parameter %r' % name)
    return val

  def get_int_param(
      self, name, default=None, required=True, validator=None, allowed=None):
    """Get the specified integer JSON parameter."""
    val = self.get_param(
        name, default=default, required=required, validator=validator,
        allowed=allowed)
    if val and type(val) != int:
      self.abort(400, msg='Parameter %r was not an int' % name)
    return val

  def get_bool_param(self, name, default=False, required=False):
    """Get the specified boolean JSON parameter."""
    val = self.get_param(name, default=default, required=required)
    if type(val) != bool:
      self.abort(400, msg='Parameter %r was not a bool' % name)
    return val

  def get_specified_feature(
      self, feature_id: Optional[int]=None) -> FeatureEntry:
    """Get the feature specified in the featureId parameter."""
    feature_id = (feature_id or
                  self.get_int_param('featureId', required=True))
    # Load feature directly from NDB so as to never get a stale cached copy.
    feature: FeatureEntry|None = FeatureEntry.get_by_id(feature_id)
    if not feature:
      self.abort(404, msg='Feature not found')
    user = self.get_current_user()
    if not permissions.can_view_feature(user, feature):
      self.abort(403, msg='Cannot view that feature')
    return feature

  def get_specified_stage(self, stage_id: int|None=None) -> Stage:
    """Get the stage specified in the stage_id parameter."""
    stage_id = stage_id or self.get_int_param('stage_id', required=True)
    stage = Stage.get_by_id(stage_id)
    if not stage:
      self.abort(404, msg='Stage not found')
    return stage

  def get_bool_arg(self, name, default=False):
    """Get the specified boolean from the query string."""
    if name not in self.request.args:
      return default
    return self.request.args[name].lower() in ('true', '1', '')

  def get_int_arg(self, name, default=None):
    """Get the specified integer from the query string."""
    val = self.request.args.get(name, default) or default
    if val is None:
      return None

    try:
      num = int(val)
    except ValueError:
      self.abort(400, msg='Request parameter %r was not an int' % name)

    if num < 0:
      self.abort(400, msg='Request parameter %r out of range: %r' % (name, val))
    return num

  def get_validated_entity(
    self,
    entity_id: int | str | None,
    entity_model: Type[M],
  ) -> M:
    """
    Fetches and validates a database entity by its ID.

    Args:
      entity_id: The ID of the entity to fetch.
      entity_model: The NDB model class of the entity.

    Returns:
      The fetched entity instance.

    Raises:
      Aborts the request with a 400 or 404 error if validation fails.
    """
    entity_name = entity_model.__name__
    if entity_id is None:
      self.abort(400, msg=f'No {entity_name} ID specified.')
    try:
      entity_id = int(entity_id)
    except:
      self.abort(400, msg=f'Invalid {entity_name} ID: {entity_id}.')

    entity = entity_model.get_by_id(entity_id)
    if entity is None:
      self.abort(404, msg=f'{entity_name} {entity_id} not found.')

    return entity

class APIHandler(BaseHandler):

  def get_headers(self):
    """Add CORS and Chrome Frame to all responses."""
    session.permanent = True
    headers = {
        'Strict-Transport-Security':
            'max-age=63072000; includeSubDomains; preload',
        'X-UA-Compatible': 'IE=Edge,chrome=1',
        'X-Frame-Options': 'DENY',
        }
    return headers

  def defensive_jsonify(self, handler_data):
    """Return a Flask Response object with a JSON string prefixed with junk."""
    body = json.dumps(handler_data, default=str)
    return flask.current_app.response_class(
        XSSI_PREFIX + body,
        mimetype=flask.current_app.json.mimetype)

  def get(self, *args, **kwargs):
    """Handle an incoming HTTP GET request."""
    headers = self.get_headers()
    handler_data = self.do_get(*args, **kwargs)
    # OpenAPI models have a to_dict attribute that should be used for
    # converting to JSON.
    if hasattr(handler_data, 'to_dict'):
      handler_data = handler_data.to_dict()
    return self.defensive_jsonify(handler_data), headers

  def post(self, *args, **kwargs):
    """Handle an incoming HTTP POST request."""
    json_body = self.request.get_json(force=True, silent=True) or {}
    logging.info('POST data is:')
    for k, v in json_body.items():
      logging.info('%r: %s', k, repr(v)[:settings.MAX_LOG_LINE])
    is_login_request = str(self.request.url_rule) in (
        '/api/v0/login', '/dev/mock_login')

    if not is_login_request:
      self.require_signed_in_and_xsrf_token()
    headers = self.get_headers()
    handler_data = self.do_post(*args, **kwargs)
    return self.defensive_jsonify(handler_data), headers

  def put(self, *args, **kwargs):
    """Handle an incoming HTTP PUT request."""
    self.require_signed_in_and_xsrf_token()
    headers = self.get_headers()
    handler_data = self.do_put(*args, **kwargs)
    return self.defensive_jsonify(handler_data), headers

  def patch(self, *args, **kwargs):
    """Handle an incoming HTTP PATCH request."""
    self.require_signed_in_and_xsrf_token()
    headers = self.get_headers()
    handler_data = self.do_patch(*args, **kwargs)
    return self.defensive_jsonify(handler_data), headers

  def delete(self, *args, **kwargs):
    """Handle an incoming HTTP DELETE request."""
    self.require_signed_in_and_xsrf_token()
    headers = self.get_headers()
    handler_data = self.do_delete(*args, **kwargs)
    return self.defensive_jsonify(handler_data), headers

  def _get_valid_methods(self):
    """For 405 responses, list methods the concrete handler implements."""
    valid_methods = ['GET']
    if self.do_post.__code__ is not APIHandler.do_post.__code__:
      valid_methods.append('POST')
    if self.do_put.__code__ is not APIHandler.do_put.__code__:
      valid_methods.append('PUT')
    if self.do_patch.__code__ is not APIHandler.do_patch.__code__:
      valid_methods.append('PATCH')
    if self.do_delete.__code__ is not APIHandler.do_delete.__code__:
      valid_methods.append('DELETE')
    return valid_methods

  def _update_last_visit_field(self, email):
    """Updates the AppUser last_visit field to log the user's last visit"""
    app_user = user_models.AppUser.get_app_user(email)
    if not app_user:
      return False
    app_user.last_visit = datetime.now()
    # Reset the flag that states determines if the user has been notified
    # of inactivity if it has been set.
    if app_user.notified_inactive is not None:
      app_user.notified_inactive = False
    app_user.put()
    return True

  def do_get(self, **kwargs):
    """Subclasses should implement this method to handle a GET request."""
    # Every API handler must handle GET.
    raise NotImplementedError()

  def do_post(self, **kwargs):
    """Subclasses should implement this method to handle a POST request."""
    self.abort(405, valid_methods=self._get_valid_methods())

  def do_put(self, **kwargs):
    """Subclasses should implement this method to handle a PUT request."""
    self.abort(405, valid_methods=self._get_valid_methods())

  def do_patch(self, **kwargs):
    """Subclasses should implement this method to handle a PATCH request."""
    self.abort(405, valid_methods=self._get_valid_methods())

  def do_delete(self, **kwargs):
    """Subclasses should implement this method to handle a DELETE request."""
    self.abort(405, valid_methods=self._get_valid_methods())

  def validate_token(self, token, email):
    """If the token is not valid, raise an exception."""
    # This is a separate method so that the refresh handler can override it.
    xsrf.validate_token(token, email)

  def require_signed_in_and_xsrf_token(self):
    """Every API POST, PUT, or DELETE must be signed in with an XSRF token."""
    user = self.get_current_user(required=True)
    token = self.request.headers.get('X-Xsrf-Token')
    if not token:
      try:
        token = self.get_param('token', required=False)
      except werkzeug.exceptions.BadRequest:
        pass  # Raised when the request has no body.
    if not token:
      self.abort(400, msg='Missing XSRF token')
    try:
      self.validate_token(token, user.email())
    except xsrf.TokenIncorrect:
      self.abort(400, msg='Invalid XSRF token')


class EntitiesAPIHandler(APIHandler):
  """Base class for APIs that handle changes to entities."""

  def abort_invalid_data_type(
      self, field: str, field_type: str, value: Any) -> None:
    """Abort the process if an invalid data type is given."""
    self.abort(400, msg=(
        f'Bad value for field {field} of type {field_type}: {value}'))

  def extract_link(self, s):
    if s:
      match_obj = URL_RE.search(str(s))
      if match_obj and match_obj.group('scheme') in ALLOWED_SCHEMES:
        link = match_obj.group()
        if not link.startswith(('http://', 'https://')):
          link = 'http://' + link
        return link
    return None

  def split_list_input(
      self,
      field: str,
      field_type: str,
      value: str,
      delimiter: str='\\r?\\n'
    ) -> list[str]:
    try:
      formatted_list = [
        x.strip() for x in re.split(delimiter, value) if x.strip()]
    except TypeError:
      self.abort_invalid_data_type(field, field_type, value)
    return formatted_list

  def update_field_value(
      self,
      entity: FeatureEntry | MilestoneSet | Stage,
      field: str,
      field_type: str,
      value: Any
    ) -> None:
    new_value = self.format_field_val(field, field_type, value)
    setattr(entity, field, new_value)

  def update_stage(
      self,
      stage: Stage,
      change_info: dict[str, Any],
      changed_fields: CHANGED_FIELDS_LIST_TYPE,
    ) -> bool:
    """Update stage fields with changes provided."""
    stage_was_updated = False
    ot_action_requested = False

    mutating_ot_milestones = any(
        isinstance(v, dict) and (
        v['form_field_name'] == 'ot_milestone_desktop_start' or
        v['form_field_name'] == 'ot_milestone_desktop_end')
        for v in change_info.values())
    ot_creation_in_progress =  (
        stage.ot_setup_status == OT_READY_FOR_CREATION or
        stage.ot_setup_status == OT_CREATION_FAILED or
        stage.ot_setup_status == OT_ACTIVATION_FAILED)
    if mutating_ot_milestones and ot_creation_in_progress:
      self.abort(400,
                 'Cannot edit OT milestones while creation is in progress.')

    # Update stage fields.
    for field, field_type in api_specs.STAGE_FIELD_DATA_TYPES:
      if field not in change_info or change_info[field] is None:
        continue
      form_field_name = change_info[field]['form_field_name']
      if form_field_name == 'ot_action_requested':
        ot_action_requested = True
      old_value = getattr(stage, field)

      new_value = change_info[field].get('value')
      self.update_field_value(stage, field, field_type, new_value)
      changed_fields.append((form_field_name, old_value, new_value))
      stage_was_updated = True

    # Update milestone fields.
    milestones = stage.milestones
    for field, field_type in api_specs.MILESTONESET_FIELD_DATA_TYPES:
      if field not in change_info or change_info[field] is None:
        continue
      if milestones is None:
        milestones = MilestoneSet()
      form_field_name = change_info[field]['form_field_name']
      old_value = getattr(milestones, field)
      new_value = change_info[field].get('value')
      self.update_field_value(milestones, field, field_type, new_value)
      changed_fields.append((form_field_name, old_value, new_value))
      stage_was_updated = True
    stage.milestones = milestones

    if stage_was_updated:
      stage.put()

    # Notify of OT creation request if one was sent.
    # This notification is for non-automated OT creation only.
    if (ot_action_requested and
        stage.stage_type in ALL_ORIGIN_TRIAL_STAGE_TYPES):
      notifier_helpers.send_ot_creation_notification(stage)

    return stage_was_updated

  def format_field_val(
      self,
      field: str,
      field_type: str,
      value: Any,
    ) -> str | int | bool | list | None:
    """Format the given feature value based on the field type."""

    # If the field is empty, no need to format.
    if value is None:
      return None

    # TODO(DanielRyanSmith): Write checks to ensure enum values are valid.
    if field_type == 'emails' or field_type == 'split_str':
      list_val = self.split_list_input(field, field_type, value, ',')
      if field == 'blink_components' and len(value) == 0:
        return [settings.DEFAULT_COMPONENT]
      return list_val
    elif field_type == 'link':
      return self.extract_link(value)
    elif field_type == 'links':
      list_val = self.split_list_input(field, field_type, value)
      # Filter out any URLs that do not conform to the proper pattern.
      return [self.extract_link(link)
              for link in list_val if link]
    elif field_type == 'int':
      # Int fields can be unset by giving null or nothing in the input field.
      if value == '' or value is None:
        return None
      try:
        return int(value)
      except ValueError:
        self.abort_invalid_data_type(field, field_type, value)
    elif field_type == 'bool':
      return bool(value)
    return str(value)


class FlaskHandler(BaseHandler):

  TEMPLATE_PATH: Optional[str] = None  # Subclasses should define this.
  HTTP_CACHE_TYPE: Optional[str] = None  # Subclasses can use 'public' or 'private'
  JSONIFY = False  # Set to True for JSON feeds.
  IS_INTERNAL_HANDLER = False  # Subclasses can skip XSRF check.

  def get_cache_headers(self):
    """Add cache control headers if HTTP_CACHE_TYPE is set."""
    if self.HTTP_CACHE_TYPE:
      directive = '%s, max-age=%s' % (
          self.HTTP_CACHE_TYPE, settings.DEFAULT_CACHE_TIME)
      return {'Cache-Control': directive}

    return {}

  def get_headers(self):
    """Add CORS and Chrome Frame to all responses."""
    session.permanent = True
    headers = {
        'Strict-Transport-Security':
            'max-age=63072000; includeSubDomains; preload',
        'X-UA-Compatible': 'IE=Edge,chrome=1',
        'X-Frame-Options': 'DENY',
        }
    headers.update(self.get_cache_headers())
    return headers

  def get_template_data(self, **kwargs):
    """Subclasses should implement this method to handle a GET request."""
    raise NotImplementedError()

  def get_template_path(self, template_data):
    """Subclasses can override their class constant via template_data."""
    if 'template_path' in template_data:
      return template_data['template_path']
    if self.TEMPLATE_PATH:
      return self.TEMPLATE_PATH
    raise ValueError(
        'No TEMPLATE_PATH was defined in %r or returned in template_data.' %
        self.__class__.__name__)

  def process_post_data(self, **kwargs):
    """Subclasses should implement this method to handle a POST request."""
    self.abort(405, msg='Unexpected HTTP method', valid_methods=['GET'])

  def get_common_data(self, path=None):
    """Return template data used on all pages, e.g., sign-in info."""
    current_path = path or flask.request.full_path
    # Used to make browser load new JS and CSS for each GAE version.
    app_version = os.environ.get('GAE_VERSION', 'Undeployed')
    common_data = {
      'prod': settings.PROD,
      'DEV_MODE': settings.DEV_MODE,
      'APP_TITLE': settings.APP_TITLE,
      'google_sign_in_client_id': settings.GOOGLE_SIGN_IN_CLIENT_ID,
      'current_path': current_path,
      'TEMPLATE_CACHE_TIME': settings.TEMPLATE_CACHE_TIME,
      'banner_message': settings.BANNER_MESSAGE,
      'banner_time': utils.get_banner_time(settings.BANNER_TIME),
      'app_version': app_version,
    }

    user = self.get_current_user()
    if user:
      gate_type = approval_defs.ShipApproval.gate_type
      approvers = approval_defs.get_approvers(gate_type)
      user_pref = user_models.UserPref.get_signed_in_user_pref()
      common_data['user'] = {
        'can_create_feature': permissions.can_create_feature(user),
        'can_edit_all': permissions.can_edit_any_feature(user),
        'is_admin': permissions.can_admin_site(user),
        'editable_features': [],
        'email': user.email(),
        'dismissed_cues': json.dumps(user_pref.dismissed_cues),
      }
      common_data['user_json'] = json.dumps(common_data['user'])
      common_data['xsrf_token'] = xsrf.generate_token(user.email())
      common_data['xsrf_token_expires'] = xsrf.token_expires_sec()
    else:
      common_data['user'] = None
      common_data['user_json'] = None
      common_data['xsrf_token'] = xsrf.generate_token(None)
      common_data['xsrf_token_expires'] = 0
    return common_data

  def render(self, template_data, template_path):
    return render_template(template_path, **template_data)

  def get(self, *args, **kwargs):
    """GET handlers can render templates, return JSON, or do redirects."""
    if self.request.host.startswith('www.'):
      location = self.request.url.replace('www.', '', 1)
      logging.info('Striping www and redirecting to %r', location)
      return self.redirect(location)
    handler_data = self.get_template_data(*args, **kwargs)
    users.refresh_user_session()

    if self.JSONIFY and type(handler_data) in (dict, list):
      headers = self.get_headers()
      return flask.jsonify(handler_data), headers

    elif type(handler_data) == dict:
      status = handler_data.get('status', 200)
      handler_data.update(self.get_common_data())
      nonce = csp.get_nonce()
      handler_data['nonce'] = nonce
      template_path = self.get_template_path(handler_data)
      template_text = self.render(handler_data, os.path.join(template_path))
      headers = self.get_headers()
      headers.update(csp.get_headers(nonce))
      return template_text, status, headers

    else:
      # handler_data is a string or redirect response object.
      return handler_data

  def post(self, *args, **kwargs):
    """POST handlers return a string, JSON, or a redirect."""
    self.require_xsrf_token()
    handler_data = self.process_post_data(*args, **kwargs)
    headers = self.get_headers()

    if self.JSONIFY and type(handler_data) in (dict, list):
      return flask.jsonify(handler_data), headers
    else:
      # handler_data is a string or redirect response object.
      return handler_data, headers

  @property
  def form(self):
    """Property for POST values dict."""
    return flask.request.form

  def require_xsrf_token(self):
    """Every UI form submission must have a XSRF token."""
    if settings.UNIT_TEST_MODE or self.IS_INTERNAL_HANDLER:
      return
    token = self.request.headers.get('X-Xsrf-Token')
    if not token:
      token = self.form.get('token')
    if not token:
      self.abort(400, msg='Missing XSRF token')
    user = self.get_current_user(required=True)
    try:
      xsrf.validate_token(token, user.email())
    except xsrf.TokenIncorrect:
      self.abort(400, msg='Invalid XSRF token')

  def require_task_header(self):
    """Abort if this is not a Google Cloud Tasks request."""
    if settings.UNIT_TEST_MODE or settings.DEV_MODE:
      return
    if 'X-AppEngine-QueueName' in self.request.headers:
      return
    if self.request.headers.get('X-Appengine-Inbound-Appid') == settings.APP_ID:
      return

    logging.info('headers lack needed header:')
    for k, v in self.request.headers:
      logging.info('%r: %r', k, v)

    self.abort(403, msg=('Lacking X-AppEngine-QueueName or '
                         'incorrect X-Appengine-Inbound-Appid headers'))

  def require_cron_header(self):
    """Abort if this is not a GAE cron request or from a site admin."""
    if settings.UNIT_TEST_MODE or settings.DEV_MODE:
      return
    if 'X-AppEngine-Cron' in self.request.headers:
      return
    user = self.get_current_user(required=True)
    if permissions.can_admin_site(user):
      return

    logging.info('non-admin and headers lack X-AppEngine-Cron:')
    for k, v in self.request.headers:
      logging.info('%r: %r', k, v)

    self.abort(403, msg='Lacking X-AppEngine-Cron or admin account')

  def split_input(self, field_name, delim='\\r?\\n'):
    """Split the input lines, strip whitespace, and skip blank lines."""
    input_text = flask.request.form.get(field_name) or ''
    return [x.strip() for x in re.split(delim, input_text)
            if x.strip()]

  def split_emails(self, param_name):
    """Split one input field and construct objects for ndb.StringProperty()."""
    addr_strs = self.split_input(param_name, delim=',')
    emails = [str(addr) for addr in addr_strs]
    return emails

  def _extract_link(self, s):
    if s:
      match_obj = URL_RE.search(str(s))
      if match_obj and match_obj.group('scheme') in ALLOWED_SCHEMES:
        link = match_obj.group()
        if not link.startswith(('http://', 'https://')):
          link = 'http://' + link
        return link

    return None

  def parse_link(self, param_name):
    s = flask.request.form.get(param_name) or None
    return self._extract_link(s)

  def parse_links(self, param_name):
    strings = self.split_input(param_name)
    links = [self._extract_link(s) for s in strings]
    links = [link for link in links if link]  # Drop any bad ones.
    return links

  def parse_int(self, param_name):
    param = flask.request.form.get(param_name) or None
    if param:
      param = int(param)
    return param


class Redirector(FlaskHandler):
  """Reusable handler that always redirects.
     Specify the location in the third part of a routing rule using:
     {'location': '/path/to/page'}."""

  def get_template_data(self, **kwargs):
    location = kwargs['location'] if 'location' in kwargs else '/'
    return flask.redirect(location), self.get_headers()


class ConstHandler(FlaskHandler):
  """Reusable handler for templates that require no page-specific logic.
     Specify the location in the third part of a routing rule using:
     {'template_path': 'path/to/template.html'}."""

  def get_template_data(self, **defaults):
    """Render a template, or return a JSON constant."""
    if defaults.get('require_signin') and not self.get_current_user():
      if 'loginStatus=False' in self.get_common_data()['current_path']:
        return {}
      return flask.redirect(settings.LOGIN_PAGE_URL), self.get_headers()
    if 'template_path' in defaults:
      template_path = defaults['template_path']
      if not template_path.endswith(('.html', '.xml')):
        self.abort(
            500, msg=f'${template_path =} does not end with .html or .xml')
      return defaults

    return flask.jsonify(defaults)


def ndb_wsgi_middleware(wsgi_app):
  """Create a new runtime context for cloud ndb for every request"""
  client = ndb.Client()

  def middleware(environ, start_response):
    with client.context():
      return wsgi_app(environ, start_response)

  return middleware


class SPAHandler(FlaskHandler):
  """Single-page app handler"""

  TEMPLATE_PATH = 'spa.html'

  def get_template_data(self, **defaults):
    return get_spa_template_data(self, defaults)


def get_spa_template_data(handler_obj, defaults):
  """Check permissions then let spa.html do its thing."""
  # Check if the page requires user to sign in
  if defaults.get('require_signin') and not handler_obj.get_current_user():
    common_data = handler_obj.get_common_data()
    if 'loginStatus=False' in common_data['current_path']:
      return {}
    return flask.redirect(settings.LOGIN_PAGE_URL), handler_obj.get_headers()

  # Check if the page requires create feature permission
  if defaults.get('require_create_feature'):
    redirect_resp = permissions.validate_feature_create_permission(handler_obj)
    if redirect_resp:
      return redirect_resp

  # Validate the user has edit permissions and redirect if needed.
  if defaults.get('require_edit_feature'):
    feature_id = defaults.get('feature_id')
    if not feature_id:
      handler_obj.abort(500, msg='Cannot get feature ID from the URL')
    redirect_resp = permissions.validate_feature_edit_permission(
        handler_obj, feature_id)
    if redirect_resp:
      return redirect_resp

  # Validate the user has admin permissions and redirect if needed.
  if defaults.get('require_admin_site'):
    user = handler_obj.get_current_user()
    # Should have already done the require_signin check.
    # If for reason, we don't let's treat it as the main 403 case.
    if not user or not permissions.can_admin_site(user):
      handler_obj.abort(403, msg='Cannot perform admin actions')

  # Validate the user has a google or chromium account and redirect if needed.
  if defaults.get('is_enterprise_page'):
    user = handler_obj.get_current_user()
    # Should have already done the require_signin check.
    # If for reason, we don't let's treat it as the main 403 case.
    if not user or not permissions.is_google_or_chromium_account(user):
      handler_obj.abort(403, msg='You cannot access this page')

  return {} # no handler_data needed to be returned



def FlaskApplication(import_name, routes, pattern_base='', debug=False):
  """Make a Flask app and add routes and handlers that work like webapp2."""

  app = flask.Flask(import_name,
    template_folder=settings.get_flask_template_path())
  app.original_wsgi_app = app.wsgi_app  # Only for unit tests.
  app.wsgi_app = ndb_wsgi_middleware(app.wsgi_app) # For Cloud NDB Context
  # For GAE legacy libraries
  app.wsgi_app = google.appengine.api.wrap_wsgi_app(app.wsgi_app)
  client = ndb.Client()
  with client.context():
    app.secret_key = secrets.get_session_secret()  # For flask.session
    app.permanent_session_lifetime = xsrf.REFRESH_TOKEN_TIMEOUT_SEC

  for i, route in enumerate(routes):
    classname = route.handler_class.__name__
    app.add_url_rule(
        pattern_base + route.path,
        endpoint=f'{classname}{i}',  # We don't use it, but it must be unique.
        view_func=route.handler_class.as_view(classname),
        defaults=route.defaults)

  # The following causes flask to print a stack trace and return 500
  # when we are running locally and a handler raises a BadRequest exception.
  # In production, it will return a status 400.
  app.config["TRAP_BAD_REQUEST_ERRORS"] = settings.DEV_MODE
  # Flask apps also have a debug setting that can be used to auto-reload
  # template source code. TODO: investigate using the setting.


  # Reject any huge POSTs
  app.config['MAX_CONTENT_LENGTH'] = settings.MAX_REQUEST_CONTENT_LENGTH

  # Set the CORS HEADERS.
  ALLOWED_API_ORIGINS = [
      'https://chromeenterprise.google',
      'https://chromeenterprise-staging.corp.google.com',
      r'https://[a-z]+-dot-xl-chrome-enterprise-staging\.uc\.r\.appspot\.com',
      r'http://localhost:\d+',
  ]
  CORS(app, resources={
      r'/data/.*': {'origins': '*'},
      r'/api/v0/features.*': {'origins': ALLOWED_API_ORIGINS,
                              'methods': ['GET']}})

  # Set cookie headers in Flask; see
  # https://flask.palletsprojects.com/en/2.0.x/config/
  # for more details.
  if not settings.DEV_MODE:
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = 'Lax'

  return app
