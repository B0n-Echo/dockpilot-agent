"""Real sign-in codes through Amazon Cognito (dp-24): passwordless email one-time codes (USER_AUTH + EMAIL_OTP).

Both calls are Cognito's public sign-in API: no AWS keys are needed, only the region and the app client id.
On with AUTH_MODE=cognito (environment or .env); otherwise the on-screen demo login is used.
"""

import json
import os
import urllib.error
import urllib.request

from dockpilot import trace

WORDS = {"CodeMismatchException": "Wrong code.",
         "ExpiredCodeException": "This code has expired. Please ask for a new one.",
         "NotAuthorizedException": "This code has expired or was used up. Please ask for a new one.",
         "TooManyFailedAttemptsException": "Too many tries. Please ask for a new one.",
         "LimitExceededException": "Too many tries. Please wait a minute and ask for a new one.",
         "TooManyRequestsException": "Too many tries. Please wait a minute and ask for a new one.",
         "UserNotFoundException": "This driver has no sign-in account yet. Please call your dispatcher."}
"""Cognito error types in our words; anything else is "Sign-in is unavailable right now"."""


class CognitoError(Exception):
    """A refused or failed Cognito call, with the reason in plain words."""


def _env(name):
    """A setting from the environment, else .env."""
    from dockpilot.llm_reader import load_env
    return os.environ.get(name) or load_env().get(name)


def enabled():
    """Is real sign-in on (AUTH_MODE=cognito)?"""
    return (_env("AUTH_MODE") or "demo").strip().lower() == "cognito"


def _call(target, body):
    """One call to Cognito's public sign-in API (10 s limit); returns its JSON or raises CognitoError."""
    region = _env("COGNITO_REGION") or "ap-south-1"
    req = urllib.request.Request(
        f"https://cognito-idp.{region}.amazonaws.com/", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/x-amz-json-1.1",
                 "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            kind = json.loads(err.read() or b"{}").get("__type", "").split("#")[-1]
        except ValueError:
            kind = ""
        print(f"cognito: {target} refused ({kind or err.code})", flush=True)  # for the server log; no secrets
        raise CognitoError(WORDS.get(kind, "Sign-in is unavailable right now. Please try again in a minute.")) from err
    except OSError as err:
        print(f"cognito: {target} failed ({type(err).__name__})", flush=True)
        raise CognitoError("Sign-in is unavailable right now. Please try again in a minute.") from err


@trace.traced("Cognito: send code", "step", show_out=lambda r: f"sent to {r[1]}")
def start_sign_in(username):
    """Ask Cognito to email a one-time code to this user; returns (Cognito's sign-in session, masked destination)."""
    r = _call("InitiateAuth", {"AuthFlow": "USER_AUTH", "ClientId": _env("COGNITO_CLIENT_ID"),
                               "AuthParameters": {"USERNAME": username, "PREFERRED_CHALLENGE": "EMAIL_OTP"}})
    if r.get("ChallengeName") != "EMAIL_OTP":
        raise CognitoError("Sign-in is unavailable right now. Please try again in a minute.")
    return r["Session"], r.get("ChallengeParameters", {}).get("CODE_DELIVERY_DESTINATION", "your email")


@trace.traced("Cognito: check code", "step", show_out=lambda ok: "accepted" if ok else "")
def finish_sign_in(username, code, session):
    """Ask Cognito to check the code; True when accepted, otherwise raises CognitoError with the reason."""
    r = _call("RespondToAuthChallenge", {"ChallengeName": "EMAIL_OTP", "ClientId": _env("COGNITO_CLIENT_ID"),
                                         "Session": session,
                                         "ChallengeResponses": {"USERNAME": username, "EMAIL_OTP_CODE": code}})
    if "AuthenticationResult" not in r:
        raise CognitoError("Sign-in is unavailable right now. Please try again in a minute.")
    return True
