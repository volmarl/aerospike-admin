# Copyright 2013-2017 Aerospike, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


class AssertLevel(object):
    CRITICAL = 0
    WARNING = 1
    INFO = 2


class AssertResultKey(object):
    FAIL_MSG = "Failmsg"
    KEYS = "Keys"
    CATEGORY = "Category"
    LEVEL = "Level"
    DESCRIPTION = "Description"
    SUCCESS_MSG = "Successmsg"
    SUCCESS = "Success"


class ParserResultType(object):
    ASSERT = "assert_result"


class HealthResultType(object):
    ASSERT = "assert_summary"
    EXCEPTIONS = "exceptions"
    EXCEPTIONS_SYNTAX = "syntax"
    EXCEPTIONS_PROCESSING = "processing"
    EXCEPTIONS_OTHER = "other"
    STATUS_COUNTERS = "status_counters"
    DEBUG_MESSAGES = "debug_messages"


class HealthResultCounter(object):
    QUERY_COUNTER = "queries"
    QUERY_SUCCESS_COUNTER = "queries_success"
    QUERY_SKIPPED_COUNTER = "queries_skipped"
    ASSERT_FAILED_COUNTER = "assert_failed"
    ASSERT_PASSED_COUNTER = "assert_passed"
    ASSERT_QUERY_COUNTER = "assert_queries"
    DEBUG_COUNTER = "debug_prints"
    SYNTAX_EXCEPTION_COUNTER = "syntax_exceptions"
    HEALTH_EXCEPTION_COUNTER = "health_exceptions"
    OTEHR_EXCEPTION_COUNTER = "other_exceptions"
