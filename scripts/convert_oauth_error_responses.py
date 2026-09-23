"""One saved OAuth 2.0 rejection response, never a client, a token request or an authentication.

The input is a HAR 1.2 file the caller already has on disk, read through the existing shared HAR intake.
The output is the producer's own error code for the endpoint role the caller declares - never a cause
read out of an HTTP status, never proof that a credential is bad, and never a physical outage.

Pinned specification text, exact:
  RFC 6749 sha256:f204fc8661d6c92d2ec6e0b54808f961a9ad26e792f57f312d9528335519bd71  S5.2 Error Response
  RFC 6750 sha256:9dc385cf4ecdd85024e5a95e447ede9230e11700bf35251906b25b37557d604b  S3, S3.1 Bearer error
"""
import argparse
import base64
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from scripts.convert_opcua_recorded import har_log
from scripts.convert_mqtt_recorded import strict_json
from scripts.convert_gpx_recorded import utc_microseconds

TOKEN_ENDPOINT = "AUTHORIZATION_SERVER_TOKEN_ENDPOINT"
RESOURCE_ENDPOINT = "PROTECTED_RESOURCE_ENDPOINT"
ENDPOINT_ROLES = {"token": TOKEN_ENDPOINT, "protected-resource": RESOURCE_ENDPOINT}
# RFC 6749 S4.1.3 / S4.3.2 / S4.4.2 / S6 are the token-request grant_type values the pinned document
# itself defines. An extension grant (S4.5) is an absolute URI and is deliberately out of scope here
# rather than being matched by a shape rule this unit has no definition for.
GRANT_TYPES = ("authorization_code", "password", "client_credentials", "refresh_token")
# RFC 6749 S5.2: the six codes, each with what that section says the server reported - not what is true.
TOKEN_ERRORS = {
    "invalid_request": "SERVER_REPORTED_THE_REQUEST_WAS_MALFORMED_OR_REPEATED_OR_MISUSED_PARAMETERS",
    "invalid_client": "SERVER_REPORTED_CLIENT_AUTHENTICATION_FAILED",
    "invalid_grant": "SERVER_REPORTED_THE_PRESENTED_GRANT_OR_REFRESH_TOKEN_WAS_NOT_ACCEPTED",
    "unauthorized_client": "SERVER_REPORTED_THE_AUTHENTICATED_CLIENT_MAY_NOT_USE_THIS_GRANT_TYPE",
    "unsupported_grant_type": "SERVER_REPORTED_IT_DOES_NOT_SUPPORT_THIS_GRANT_TYPE",
    "invalid_scope": "SERVER_REPORTED_THE_REQUESTED_SCOPE_WAS_NOT_ACCEPTED",
}
# RFC 6750 S3.1: the three resource-server codes and the status each SHOULD carry.
RESOURCE_ERRORS = {
    "invalid_request": "RESOURCE_SERVER_REPORTED_THE_REQUEST_WAS_MALFORMED_OR_MISUSED_THE_ACCESS_TOKEN",
    "invalid_token": "RESOURCE_SERVER_REPORTED_THE_PRESENTED_ACCESS_TOKEN_WAS_NOT_ACCEPTED",
    "insufficient_scope": "RESOURCE_SERVER_REPORTED_THE_REQUEST_NEEDS_MORE_PRIVILEGE_THAN_THE_TOKEN_CARRIED",
}
RESOURCE_SHOULD_STATUS = {"invalid_request": 400, "invalid_token": 401, "insufficient_scope": 403}
ERRORS_BY_ROLE = {TOKEN_ENDPOINT: TOKEN_ERRORS, RESOURCE_ENDPOINT: RESOURCE_ERRORS}
# RFC 6749 S5.2 (and RFC 6750 S3 for the challenge attribute): "MUST NOT include characters outside the
# set %x20-21 / %x23-5B / %x5D-7E". The set excludes both the quote and the backslash, so a challenge
# value that conforms cannot contain an escape.
NQCHAR = r"[\x20-\x21\x23-\x5b\x5d-\x7e]"
ERROR_VALUE = re.compile(NQCHAR + "{1,256}")
KNOWN_MEMBERS = ("error", "error_description", "error_uri")
# RFC 6750 S3 says the challenge scheme MUST be "Bearer" and uses the framework of HTTP/1.1 [RFC2617].
# RFC 2617 sha256:cf5492136782d9e9fce492c254ca39e2c93328cf38c4dd006039abb5d8e27ba7 S1.2 defines that
# framework exactly, and is what the parser below implements:
#   auth-scheme = token ; auth-param = token "=" ( token | quoted-string )
#   challenge   = auth-scheme 1*SP 1#auth-param
#   "an extensible, case-insensitive token to identify the authentication scheme"
#   "The realm directive (case-insensitive) ... The realm value (case-sensitive)"
#   Note: "User agents will need to take special care in parsing the WWW-Authenticate ... header field
#   value if it contains more than one challenge ... since the contents of a challenge may itself
#   contain a comma-separated list of authentication parameters."
# So: the scheme and the attribute names are compared case-insensitively, the values are not, and a
# field that carries more than one challenge is refused here instead of being half-read.
TOKEN_SEPARATORS = '()<>@,;:\\"/[]?={} \t'
CHALLENGE_ATTRIBUTES = ("realm", "scope", "error", "error_description", "error_uri")
MAX_CHALLENGE_PARAMS = 64
JSON_MIME = re.compile(r"application/(?:json|[A-Za-z0-9.!#$&^_+-]+\+json)(?:\s*;.*)?")
MAX_BODY_BYTES = 64 * 1024
MAX_HEADERS = 1024
MAX_CHALLENGE_BYTES = 4096
MAX_UNKNOWN_MEMBERS = 64
NO_ERROR = "NO_ERROR_REPORTED"
BODY_ERROR = "JSON_BODY_ERROR_MEMBER_RFC6749_5_2"
CHALLENGE_ERROR_SOURCE = "BEARER_CHALLENGE_ERROR_ATTRIBUTE_RFC6750_3"
UNQUALIFIED = ("UNQUALIFIED_NO_PROTOCOL_ERROR_REPORTED_SO_THE_STATUS_ALONE_NAMES_NO_OAUTH_REJECTION")
DEFINED = "DEFINED_BY_THE_PINNED_SECTION_FOR_THIS_ENDPOINT_ROLE"
OTHER_ROLE = "NOT_DEFINED_FOR_THIS_ENDPOINT_ROLE_BY_THE_PINNED_SECTIONS_RETAINED_AS_REPORTED"
UNREGISTERED = "NOT_DEFINED_BY_THE_PINNED_SECTIONS_RETAINED_AS_REPORTED"
CLOCK_BASIS = "REPORTED_REQUEST_START_NOT_MEASUREMENT_OR_RESPONSE_END"
# The common reader refuses quoted CSV input, so every emitted value stays free of commas and quotes.
REJECTION_BASIS = ("the endpoint's own reported protocol rejection; never proof that a credential or token"
                   " or account or permission is actually bad; never a network or power or service outage;"
                   " never a decision about a person")
IDENTITY_BASIS = ("endpoint role and grant context are caller-declared; not authenticated and not read"
                  " from a URL; no client or user or account or device identity is resolved here")
FIELDS = ["record_time_us", "har_entry_index", "har_source_sha256", "har_clock_basis",
          "oauth_endpoint_role", "oauth_grant_type_declared", "http_status_reported",
          "oauth_status_agreement", "oauth_error_source", "oauth_error_reported",
          "oauth_error_registry_disposition", "oauth_rejection_reported",
          "oauth_error_description_disposition", "oauth_error_uri_disposition",
          "oauth_scope_attribute_disposition", "oauth_unknown_member_count",
          "oauth_unknown_challenge_attribute_count", "oauth_error_body_sha256",
          "oauth_challenge_scheme_reported", "oauth_rejection_basis", "oauth_identity_basis"]


def saved_error_body(response):
    """The decoded JSON error object of a token-endpoint rejection, exactly as RFC 6749 S5.2 serialises it.

    The shared HAR route's own body rules are reused: HAR text is already decompressed and unchunked by
    the exporter, and a base64 `encoding` is the only transfer form defined for it.
    """
    content = response.get("content")
    if not isinstance(content, dict):
        raise ValueError("saved response content required")
    mime = content.get("mimeType")
    if not isinstance(mime, str) or not JSON_MIME.fullmatch(mime):
        # S5.2 serialises the parameters as application/json. A rejection carried in some other media
        # type is not this function's input and is refused rather than sniffed.
        raise ValueError("RFC6749 5.2 error responses are JSON; this body declares another media type")
    body = content.get("text")
    if not isinstance(body, str):
        raise ValueError("saved error body text required")
    encoding = content.get("encoding")
    if encoding is not None:
        if encoding != "base64":
            raise ValueError("unsupported HAR body encoding")
        body = base64.b64decode(body, validate=True).decode("utf-8")
    if len(body.encode()) > MAX_BODY_BYTES:
        raise ValueError("saved error body exceeds the selected bound")
    parsed = strict_json(body)
    if not isinstance(parsed, dict):
        raise ValueError("RFC6749 5.2 error response must be a JSON object")
    return body, parsed


def token_char(char):
    """RFC 2617 S1.2 relies on the HTTP/1.1 token: a CHAR that is neither a CTL nor a separator."""
    return " " < char < "\x7f" and char not in TOKEN_SEPARATORS


def bearer_challenge_params(value):
    """Parse ONE Bearer challenge into its auth-params at the boundaries RFC 2617 S1.2 defines.

    Nothing here searches the field for a substring. `error=` written inside a quoted realm, or hidden
    behind a quoted pair, is part of that value and can never become a reported code; a second challenge
    on the same field is refused rather than half-read, exactly as that section's own Note warns; and an
    auth-param whose syntax does not parse is refused instead of quietly reading as absent.

    Names are lowercased because the section calls the directive case-insensitive; values are returned
    untouched because it calls the realm value case-sensitive. No offending text is ever put into an
    exception message - a saved challenge may carry a realm or a scope that the caller keeps private.
    """
    position, length = 0, len(value)
    while position < length and value[position] in " \t":
        position += 1
    start = position
    while position < length and token_char(value[position]):
        position += 1
    if value[start:position].lower() != "bearer":
        raise ValueError("only the Bearer challenge scheme of RFC6750 3 is mapped here")
    if position < length and value[position] not in " \t,":
        raise ValueError("malformed challenge: the scheme must be followed by a space")
    params, separated = {}, True
    while True:
        while position < length and value[position] in " \t,":
            separated = separated or value[position] == ","
            position += 1
        # `separated` is true before the first auth-param and again after every comma that follows one.
        if position == length:
            return params
        if not separated:
            # `1#auth-param` is a comma-separated list. Two params run together are not resolved by
            # guessing where one ends, because that is how a planted value gets read as a parameter.
            raise ValueError("malformed challenge: auth-params must be comma-separated")
        start = position
        while position < length and token_char(value[position]):
            position += 1
        name = value[start:position]
        if not name:
            raise ValueError("malformed challenge: an auth-param name is required")
        while position < length and value[position] in " \t":
            position += 1
        if position == length or value[position] != "=":
            # A bare token where an auth-param belongs is the next challenge's scheme. The section's
            # own Note says such a field needs special care, so it is refused, never guessed at.
            raise ValueError("a second challenge or an unparsable auth-param is not resolved here")
        position += 1
        while position < length and value[position] in " \t":
            position += 1
        if position == length:
            raise ValueError("malformed challenge: an auth-param value is required")
        if value[position] == '"':
            position, parsed = position + 1, []
            while True:
                if position == length:
                    raise ValueError("malformed challenge: a quoted value is not terminated")
                char = value[position]
                position += 1
                if char == "\\":
                    # A quoted pair: the next character is data, including a quote or a backslash.
                    if position == length:
                        raise ValueError("malformed challenge: a quoted pair is not terminated")
                    parsed.append(value[position])
                    position += 1
                    continue
                if char == '"':
                    break
                parsed.append(char)
            parsed = "".join(parsed)
        else:
            start = position
            while position < length and token_char(value[position]):
                position += 1
            parsed = value[start:position]
            if not parsed:
                raise ValueError("malformed challenge: an auth-param value is required")
        key = name.lower()
        if key in params:
            raise ValueError("RFC6750 3 forbids a repeated challenge attribute")
        params[key] = parsed
        separated = False
        if len(params) > MAX_CHALLENGE_PARAMS:
            raise ValueError("saved challenge exceeds the selected auth-param bound")


def challenge_error(response):
    """The `error` attribute of the saved Bearer challenge, or absence - never the realm or scope value.

    RFC 6750 S3 requires the scheme "Bearer" and says error, error_description and error_uri MUST NOT
    appear more than once, so a repeated attribute is refused rather than resolved by picking one.
    """
    headers = response.get("headers")
    if not isinstance(headers, list):
        raise ValueError("saved response headers required")
    if len(headers) > MAX_HEADERS:
        raise ValueError("saved response exceeds the selected header bound")
    challenges = []
    for header in headers:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            raise ValueError("saved header name required")
        if header["name"].lower() == "www-authenticate":
            value = header.get("value")
            if not isinstance(value, str):
                raise ValueError("saved challenge value required")
            challenges.append(value)
    if not challenges:
        # S3.1: a request that lacks authentication information SHOULD NOT be answered with an error
        # code, so an absent challenge is an ordinary reported outcome, not a malformed input.
        return None, "", "", ""
    if len(challenges) > 1:
        # RFC 2617 S1.2's Note covers this case too: more than one such field needs special care.
        raise ValueError("more than one saved WWW-Authenticate challenge is not resolved here")
    challenge = challenges[0]
    if len(challenge.encode()) > MAX_CHALLENGE_BYTES:
        raise ValueError("saved challenge exceeds the selected bound")
    params = bearer_challenge_params(challenge)
    present = [name for name in ("error_description", "error_uri", "scope") if name in params]
    # Counted, never copied: an attribute the pinned sections do not define keeps its name and its
    # value in the caller's retained original only.
    unknown = sum(name not in CHALLENGE_ATTRIBUTES for name in params)
    return params.get("error"), "BEARER", " ".join(present), unknown


def transportable(error):
    """The reported code must survive the common reader's unquoted CSV form, or the row is refused.

    The sections' character set admits a comma, which the reader rejects as quoted input. Such a code is
    refused with its own reason rather than rewritten, escaped or dropped: the producer's value is either
    carried exactly or not carried at all.
    """
    if "," in error or '"' in error:
        raise ValueError("reported error contains a separator the unquoted common CSV form cannot carry")
    return error


def status_agreement(role, status, error):
    """What the pinned section says about this status for this code - reported, never enforced."""
    if error is None:
        return "STATUS_NOT_QUALIFIED_BY_THE_SECTION_BECAUSE_NO_ERROR_CODE_WAS_REPORTED"
    if role == TOKEN_ENDPOINT:
        if status == 400:
            return "STATUS_IS_THE_SECTIONS_DEFAULT_400_FOR_A_TOKEN_ERROR_RESPONSE"
        if status == 401 and error == "invalid_client":
            return "SECTION_EXPLICITLY_ALLOWS_401_FOR_INVALID_CLIENT"
        return "STATUS_IS_NOT_THE_SECTIONS_DEFAULT_AND_IS_CARRIED_AS_REPORTED"
    expected = RESOURCE_SHOULD_STATUS.get(error)
    if expected is None:
        return "SECTION_STATES_NO_STATUS_FOR_THIS_CODE_SO_THE_STATUS_IS_CARRIED_AS_REPORTED"
    if status == expected:
        return "STATUS_MATCHES_THE_SECTIONS_SHOULD_FOR_THIS_CODE"
    return "STATUS_DIFFERS_FROM_THE_SECTIONS_SHOULD_AND_IS_CARRIED_AS_REPORTED"


def convert_saved_rejection(text, entry_index, endpoint_role, grant_type=None):
    """One saved rejection to one Observation row; nothing is fetched and no secret is copied out."""
    role = ENDPOINT_ROLES.get(endpoint_role)
    if role is None:
        raise ValueError("explicit declared endpoint role required")
    if role == TOKEN_ENDPOINT:
        if grant_type not in GRANT_TYPES:
            # The grant the request used is context the response body does not carry; it is declared or
            # the row is not produced. An extension grant URI (S4.5) is out of scope, not guessed.
            raise ValueError("token-endpoint rejections require one declared RFC6749 grant_type")
    elif grant_type is not None:
        raise ValueError("a protected-resource rejection carries no token-request grant type")
    log = har_log(text)
    if type(entry_index) is not int or type(entry_index) is bool or not 0 <= entry_index < len(log["entries"]):
        raise ValueError("explicit existing HAR entry index required")
    entry = log["entries"][entry_index]
    response = entry["response"]
    status = response["status"]
    if type(status) is not int or type(status) is bool or not 400 <= status <= 599:
        # A reported rejection, not any saved HTTP outcome: the existing shared route already reports
        # every entry's status class, and it is not this converter's job to repeat it.
        raise ValueError("a saved OAuth rejection must report a 400..599 status")
    row = dict.fromkeys(FIELDS, "")
    row.update(record_time_us=utc_microseconds(entry["startedDateTime"]), har_entry_index=entry_index,
               har_source_sha256="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
               har_clock_basis=CLOCK_BASIS, oauth_endpoint_role=role, http_status_reported=status,
               oauth_grant_type_declared=grant_type or "NOT_APPLICABLE_TO_THIS_ENDPOINT_ROLE",
               oauth_rejection_basis=REJECTION_BASIS, oauth_identity_basis=IDENTITY_BASIS)
    if role == TOKEN_ENDPOINT:
        body, parsed = saved_error_body(response)
        row["oauth_error_body_sha256"] = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        unknown = [name for name in parsed if name not in KNOWN_MEMBERS]
        if len(unknown) > MAX_UNKNOWN_MEMBERS:
            raise ValueError("saved error object exceeds the unknown-member bound")
        # Counted, never copied: an unrecognised member's name and value stay in the retained original.
        row["oauth_unknown_member_count"] = len(unknown)
        for member, column in (("error_description", "oauth_error_description_disposition"),
                               ("error_uri", "oauth_error_uri_disposition")):
            if member not in parsed:
                row[column] = "ABSENT"
                continue
            if not isinstance(parsed[member], str):
                raise ValueError("RFC6749 5.2 optional error members are JSON strings")
            row[column] = "REPORTED_BUT_DELIBERATELY_NOT_COPIED_OUT_OF_THE_RETAINED_ORIGINAL"
        row["oauth_scope_attribute_disposition"] = "NOT_APPLICABLE_TO_A_TOKEN_ERROR_RESPONSE"
        row["oauth_challenge_scheme_reported"] = "NOT_READ_FOR_A_TOKEN_ERROR_RESPONSE"
        # Absence and an explicit null are different statements: a member that is present must be the
        # single ASCII code S5.2 defines, so a null is refused rather than read as "no code reported".
        error = None
        if "error" in parsed:
            error = parsed["error"]
            if not isinstance(error, str) or not ERROR_VALUE.fullmatch(error):
                raise ValueError("reported error must be a nonempty value in the sections character set")
            transportable(error)
    else:
        error, scheme, present, unknown = challenge_error(response)
        row["oauth_challenge_scheme_reported"] = scheme or "NO_CHALLENGE_REPORTED"
        row["oauth_unknown_challenge_attribute_count"] = unknown if scheme else ""
        row["oauth_error_description_disposition"] = (
            "REPORTED_BUT_DELIBERATELY_NOT_COPIED_OUT_OF_THE_RETAINED_ORIGINAL"
            if "error_description" in present else "ABSENT")
        row["oauth_error_uri_disposition"] = (
            "REPORTED_BUT_DELIBERATELY_NOT_COPIED_OUT_OF_THE_RETAINED_ORIGINAL"
            if "error_uri" in present else "ABSENT")
        row["oauth_scope_attribute_disposition"] = (
            "REQUIRED_SCOPE_HINT_REPORTED_BUT_ITS_VALUE_IS_NOT_COPIED"
            if "scope" in present else "ABSENT")
        row["oauth_error_body_sha256"] = "BODY_NOT_READ_FOR_A_PROTECTED_RESOURCE_REJECTION"
        if error is not None:
            if not ERROR_VALUE.fullmatch(error):
                raise ValueError("reported error must be a nonempty value in the sections character set")
            transportable(error)
    if error is None:
        # The decisive case: a 401 or 403 on its own names no OAuth rejection, and RFC 6750 S3.1 says
        # so itself for a request that carried no authentication information.
        row.update(oauth_error_source=NO_ERROR, oauth_error_reported="",
                   oauth_error_registry_disposition="NO_ERROR_CODE_WAS_REPORTED_SO_NONE_IS_LOOKED_UP",
                   oauth_rejection_reported=UNQUALIFIED)
    else:
        row["oauth_error_source"] = BODY_ERROR if role == TOKEN_ENDPOINT else CHALLENGE_ERROR_SOURCE
        row["oauth_error_reported"] = error
        defined = ERRORS_BY_ROLE[role]
        other = TOKEN_ERRORS if role == RESOURCE_ENDPOINT else RESOURCE_ERRORS
        if error in defined:
            row["oauth_error_registry_disposition"] = DEFINED
            row["oauth_rejection_reported"] = defined[error]
        else:
            # Retained exactly as reported. A code the other section defines is not borrowed for this
            # role, and an unregistered code is never mapped onto a neighbouring meaning.
            row["oauth_error_registry_disposition"] = OTHER_ROLE if error in other else UNREGISTERED
            row["oauth_rejection_reported"] = "REPORTED_CODE_NOT_NAMED_BY_THE_PINNED_SECTIONS"
    row["oauth_status_agreement"] = status_agreement(role, status, error)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    report = dict(records=1, har_entry_index=entry_index, har_entries=len(log["entries"]),
                  har_unselected_entries=len(log["entries"]) - 1,
                  har_source_sha256=row["har_source_sha256"], endpoint_role=role,
                  grant_type_declared=row["oauth_grant_type_declared"],
                  http_status_reported=status, error_source=row["oauth_error_source"],
                  error_reported=row["oauth_error_reported"],
                  error_registry_disposition=row["oauth_error_registry_disposition"],
                  unknown_error_members=row["oauth_unknown_member_count"],
                  unknown_challenge_attributes=row["oauth_unknown_challenge_attribute_count"],
                  clock="Unknown",
                  clock_basis=CLOCK_BASIS, rejection_basis=REJECTION_BASIS,
                  identity_basis=IDENTITY_BASIS,
                  specification_pin=("RFC6749 sha256:f204fc8661d6c92d2ec6e0b54808f961a9ad26e792f57f312d95"
                                     "28335519bd71 S5.2; RFC6750 sha256:9dc385cf4ecdd85024e5a95e447ede92"
                                     "30e11700bf35251906b25b37557d604b S3 and S3.1"),
                  retention=("the original HAR stays with the caller; no url, header value, cookie,"
                             " request or response body text, token, code, secret, error_description,"
                             " error_uri, realm or scope value is copied into this output"))
    return output.getvalue(), report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--har-entry", type=int, required=True,
                        help="the exact saved entry to map; rejections are never searched for")
    parser.add_argument("--endpoint-role", choices=sorted(ENDPOINT_ROLES), required=True)
    parser.add_argument("--grant-type", choices=GRANT_TYPES,
                        help="required for the token endpoint, refused for a protected resource")
    args = parser.parse_args()
    with args.input.open("rb") as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("input exceeds bound")
    output, report = convert_saved_rejection(raw.decode("utf-8"), args.har_entry, args.endpoint_role,
                                             args.grant_type)
    with args.output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(output)
    with args.report.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
