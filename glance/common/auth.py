# Copyright 2011 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
This auth module is intended to allow OpenStack client-tools to select from a
variety of authentication strategies, including NoAuth (the default), and
Keystone (an identity management system).

::

   > auth_plugin = AuthPlugin(creds)

   > auth_plugin.authenticate()

   > auth_plugin.auth_token
    abcdefg

   > auth_plugin.management_url
   http://service_endpoint/

"""

import http.client as http
import urllib.parse as urlparse

import httplib2
from keystoneclient import service_catalog as ks_service_catalog
from oslo_serialization import jsonutils

from glance.common import exception
from glance.i18n import _


class BaseStrategy(object):
    def __init__(self):
        self.auth_token = None
        # TODO(sirp): Should expose selecting public/internal/admin URL.
        self.management_url = None

    def authenticate(self):
        raise NotImplementedError

    @property
    def is_authenticated(self):
        raise NotImplementedError

    @property
    def strategy(self):
        raise NotImplementedError


class NoAuthStrategy(BaseStrategy):
    def authenticate(self):
        pass

    @property
    def is_authenticated(self):
        return True

    @property
    def strategy(self):
        return 'noauth'


class KeystoneStrategy(BaseStrategy):
    MAX_REDIRECTS = 10

    def __init__(self, creds, insecure=False, configure_via_auth=True):
        self.creds = creds
        self.insecure = insecure
        self.configure_via_auth = configure_via_auth
        super(KeystoneStrategy, self).__init__()

    def check_auth_params(self):
        # Ensure that supplied credential parameters are as required
        for required in ('username', 'password', 'auth_url',
                         'strategy'):
            if self.creds.get(required) is None:
                raise exception.MissingCredentialError(required=required)
        if self.creds['strategy'] != 'keystone':
            raise exception.BadAuthStrategy(expected='keystone',
                                            received=self.creds['strategy'])
        # For v2.0 also check tenant is present
        if self.creds['auth_url'].rstrip('/').endswith('v2.0'):
            if self.creds.get("tenant") is None:
                raise exception.MissingCredentialError(required='tenant')

        # For v3 also check project is present
        if self.creds['auth_url'].rstrip('/').endswith('v3'):
            if self.creds.get("project") is None:
                raise exception.MissingCredentialError(required='project')

    def authenticate(self):
        """Authenticate with the Keystone service.

        There are a few scenarios to consider here:

        1. Which version of Keystone are we using? v1 which uses headers to
           pass the credentials, or v2 which uses a JSON encoded request body?

        2. Keystone may respond back with a redirection using a 305 status
           code.

        3. We may attempt a v1 auth when v2 is what's called for. In this
           case, we rewrite the url to contain /v2.0/ and retry using the v2
           protocol.
        """
        def _authenticate(auth_url):
            # If OS_AUTH_URL is missing a trailing slash add one
            if not auth_url.endswith('/'):
                auth_url += '/'

            token_url = urlparse.urljoin(auth_url, "tokens")
            # 1. Check Keystone version
            is_v2 = auth_url.rstrip('/').endswith('v2.0')
            is_v3 = auth_url.rstrip('/').endswith('v3')
            if is_v3:
                token_url = urlparse.urljoin(auth_url, "auth/tokens")
                self._v3_auth(token_url)
            elif is_v2:
                self._v2_auth(token_url)
            else:
                self._v1_auth(token_url)

        self.check_auth_params()
        auth_url = self.creds['auth_url']
        for redirect_iter in range(self.MAX_REDIRECTS):
            try:
                _authenticate(auth_url)
            except exception.AuthorizationRedirect as e:
                # 2. Keystone may redirect us
                auth_url = e.url
            except exception.AuthorizationFailure:
                # 3. In some configurations nova makes redirection to
                # v2.0 keystone endpoint. Also, new location does not
                # contain real endpoint, only hostname and port.
                if 'v2.0' not in auth_url:
                    auth_url = urlparse.urljoin(auth_url, 'v2.0/')
            else:
                # If we successfully auth'd, then memorize the correct auth_url
                # for future use.
                self.creds['auth_url'] = auth_url
                break
        else:
            # Guard against a redirection loop
            raise exception.MaxRedirectsExceeded(redirects=self.MAX_REDIRECTS)

    def _v1_auth(self, token_url):
        creds = self.creds

        headers = {
            'X-Auth-User': creds['username'],
            'X-Auth-Key': creds['password']
        }

        tenant = creds.get('tenant')
        if tenant:
            headers['X-Auth-Tenant'] = tenant

        resp, resp_body = self._do_request(token_url, 'GET', headers=headers)

        def _management_url(self, resp):
            for url_header in ('x-image-management-url',
                               'x-server-management-url',
                               'x-glance'):
                try:
                    return resp[url_header]
                except KeyError as e:
                    not_found = e
            raise not_found

        if resp.status in (http.OK, http.NO_CONTENT):
            try:
                if self.configure_via_auth:
                    self.management_url = _management_url(self, resp)
                self.auth_token = resp['x-auth-token']
            except KeyError:
                raise exception.AuthorizationFailure()
        elif resp.status == http.USE_PROXY:
            raise exception.AuthorizationRedirect(uri=resp['location'])
        elif resp.status == http.BAD_REQUEST:
            raise exception.AuthBadRequest(url=token_url)
        elif resp.status == http.UNAUTHORIZED:
            raise exception.NotAuthenticated()
        elif resp.status == http.NOT_FOUND:
            raise exception.AuthUrlNotFound(url=token_url)
        else:
            raise Exception(_('Unexpected response: %s') % resp.status)

    def _v3_auth(self, token_url):
        creds = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": self.creds['username'],
                            "domain": {"id": self.creds['user_domain_id']},
                            "password": self.creds['password']
                        }
                    }
                },
                "scope": {
                    "project": {
                        "name": self.creds['project'],
                        "domain": {
                            "id": self.creds['project_domain_id']
                        }
                    }
                }
            }
        }

        headers = {'Content-Type': 'application/json'}
        req_body = jsonutils.dumps(creds)

        resp, resp_body = self._do_request(
            token_url, 'POST', headers=headers, body=req_body)
        resp_body = jsonutils.loads(resp_body)

        if resp.status == 201:
            resp_auth = resp['x-subject-token']
            creds_region = self.creds.get('region')
            if self.configure_via_auth:
                endpoint = get_endpoint(resp_body['token']['catalog'],
                                        endpoint_region=creds_region)
                self.management_url = endpoint
            self.auth_token = resp_auth
        elif resp.status == 305:
            raise exception.RedirectException(resp['location'])
        elif resp.status == 400:
            raise exception.AuthBadRequest(url=token_url)
        elif resp.status == 401:
            raise Exception(_('Unexpected response: %s') % resp.status)

    def _v2_auth(self, token_url):

        creds = self.creds

        creds = {
            "auth": {
                "tenantName": creds['tenant'],
                "passwordCredentials": {
                    "username": creds['username'],
                    "password": creds['password']
                }
            }
        }

        headers = {'Content-Type': 'application/json'}
        req_body = jsonutils.dumps(creds)

        resp, resp_body = self._do_request(
            token_url, 'POST', headers=headers, body=req_body)

        if resp.status == http.OK:
            resp_auth = jsonutils.loads(resp_body)['access']
            creds_region = self.creds.get('region')
            if self.configure_via_auth:
                endpoint = get_endpoint(resp_auth['serviceCatalog'],
                                        endpoint_region=creds_region)
                self.management_url = endpoint
            self.auth_token = resp_auth['token']['id']
        elif resp.status == http.USE_PROXY:
            raise exception.RedirectException(resp['location'])
        elif resp.status == http.BAD_REQUEST:
            raise exception.AuthBadRequest(url=token_url)
        elif resp.status == http.UNAUTHORIZED:
            raise exception.NotAuthenticated()
        elif resp.status == http.NOT_FOUND:
            raise exception.AuthUrlNotFound(url=token_url)
        else:
            raise Exception(_('Unexpected response: %s') % resp.status)

    @property
    def is_authenticated(self):
        return self.auth_token is not None

    @property
    def strategy(self):
        return 'keystone'

    def _do_request(self, url, method, headers=None, body=None):
        headers = headers or {}
        conn = httplib2.Http()
        conn.force_exception_to_status_code = True
        conn.disable_ssl_certificate_validation = self.insecure
        headers['User-Agent'] = 'glance-client'
        resp, resp_body = conn.request(url, method, headers=headers, body=body)
        return resp, resp_body


def get_plugin_from_strategy(strategy, creds=None, insecure=False,
                             configure_via_auth=True):
    if strategy == 'noauth':
        return NoAuthStrategy()
    elif strategy == 'keystone':
        return KeystoneStrategy(creds, insecure,
                                configure_via_auth=configure_via_auth)
    else:
        raise Exception(_("Unknown auth strategy '%s'") % strategy)


def get_endpoint(service_catalog, service_type='image', endpoint_region=None,
                 endpoint_type='publicURL'):
    """
    Select an endpoint from the service catalog

    We search the full service catalog for services
    matching both type and region. If the client
    supplied no region then any 'image' endpoint
    is considered a match. There must be one -- and
    only one -- successful match in the catalog,
    otherwise we will raise an exception.
    """
    endpoints = ks_service_catalog.ServiceCatalogV2(
        {'serviceCatalog': service_catalog}
    ).get_urls(service_type=service_type,
               region_name=endpoint_region,
               endpoint_type=endpoint_type)
    if endpoints is None:
        raise exception.NoServiceEndpoint()
    elif len(endpoints) == 1:
        return endpoints[0]
    else:
        raise exception.RegionAmbiguity(region=endpoint_region)
