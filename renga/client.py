# -*- coding: utf-8 -*-
#
# Copyright 2017 - Swiss Data Science Center (SDSC)
# A partnership between École Polytechnique Fédérale de Lausanne (EPFL) and
# Eidgenössische Technische Hochschule Zürich (ETHZ).
#
# Licensed under the Apache License, Version 2.0 (the "License");
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
"""Python SDK client for Renga platform."""

import os

from .api import APIClient


class RengaClient(object):
    """A client for communicating with a Renga platform.

    Example:

        >>> import renga
        >>> client = renga.RengaClient('http://localhost')

    """

    def __init__(self, *args, **kwargs):
        """Create a Renga API client."""
        self.api = APIClient(*args, **kwargs)

    @classmethod
    def from_env(cls, environment=None):
        """Return a client configured from environment variables.

        .. envvar:: RENGA_ENDPOINT

            The URL to the Renga platform.

        .. envvar:: RENGA_ACCESS_TOKEN

            An access token obtained from Renga authentication service.

        Example:

            >>> import renga
            >>> client = renga.from_env()

        """
        if not environment:
            environment = os.environ

        endpoint = environment.get('RENGA_ENDPOINT', '')
        access_token = environment.get('RENGA_ACCESS_TOKEN')
        client = cls(endpoint=endpoint, token={'access_token': access_token})

        # FIXME temporary solution until the execution id is moved to the token
        execution_id = environment.get('RENGA_VERTEX_ID')
        if execution_id:
            client.api.headers['Renga-Deployer-Execution'] = execution_id

        return client

    @property
    def contexts(self):
        """An object for managing contexts on the server.

        See the :doc:`contexts documentation <contexts>` for full details.
        """
        from .models.deployer import ContextCollection
        return ContextCollection(client=self)

    @property
    def projects(self):
        """An object for managing projects on the server.

        See the :doc:`projects documentation <projects>` for full details.
        """
        from .models.projects import ProjectCollection
        return ProjectCollection(client=self)

    @property
    def buckets(self):
        """An object for managing buckets on the server.

        See the :doc:`buckets documentation <buckets>` for full details.
        """
        from .models.storage import BucketCollection
        return BucketCollection(client=self)


from_env = RengaClient.from_env
