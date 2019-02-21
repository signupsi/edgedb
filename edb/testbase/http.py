#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2019-present MagicStack Inc. and the EdgeDB authors.
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
#


import json
import urllib.parse
import urllib.request

import edgedb

from edb.server import cluster

from . import server


class GraphQLTestCase(server.QueryTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.http_port = cluster.find_available_port()
        dbname = cls.get_database_name()

        cls.loop.run_until_complete(
            cls.con.execute(
                f'''
                    SET SYSTEM CONFIG ports += $$
                    {{
                        "protocol": "http+graphql",
                        "database": "{dbname}",
                        "address": "127.0.0.1",
                        "port": {cls.http_port},
                        "user": "http",
                        "concurrency": 4
                    }}
                    $$;
                '''))

        cls.http_addr = f'http://127.0.0.1:{cls.http_port}'

    def graphql_query(self, query, *, operation_name=None,
                      use_http_post=True):
        req_data = {
            'query': query
        }

        if operation_name is not None:
            req_data['operationName'] = operation_name

        if use_http_post:
            req = urllib.request.Request(self.http_addr)
            req.add_header('Content-Type', 'application/json')
            response = urllib.request.urlopen(
                req, json.dumps(req_data).encode())
            resp_data = json.loads(response.read())
        else:
            response = urllib.request.urlopen(
                f'{self.http_addr}/?{urllib.parse.urlencode(req_data)}')
            resp_data = json.loads(response.read())

        if 'data' in resp_data:
            return resp_data['data']

        err = resp_data['errors'][0]

        typename, msg = err['message'].split(':', 1)
        msg = msg.strip()

        ex = getattr(edgedb, typename)(msg)

        if 'locations' in err:
            # XXX Fix this when LSP "location" objects are implemented
            ex._attrs['L'] = err['locations'][0]['line']
            ex._attrs['C'] = err['locations'][0]['column']

        raise ex

    def assert_graphql_query_result(self, query, result, *,
                                    msg=None, sort=None,
                                    operation_name=None,
                                    use_http_post=True):
        res = self.graphql_query(
            query,
            operation_name=operation_name,
            use_http_post=use_http_post)

        if sort is not None:
            # GQL will always have a single object returned. The data is
            # in the top-level fields, so that's what needs to be sorted.
            for r in res.values():
                self._sort_results(r, sort)

        self._assert_data_shape(res, result, message=msg)
        return res