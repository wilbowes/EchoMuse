"""
em_ingressauth.py — whether a request may be authenticated by Home Assistant.

Under the add-on, Home Assistant has already authenticated the person before
the request reaches us, so a second EchoMuse password is a lock on a door
that is already locked. Supervisor forwards the authenticated user as
X-Remote-User-Id (plus optional name headers) and, importantly, **strips any
incoming headers of those names** before proxying — so the values cannot be
supplied by the client and can be trusted.

They can be trusted *only* on a request that genuinely came through
Supervisor. That is the entire security content of this module, and it is
why the decision is a pure function with tests rather than an `if` in a
handler: the same header on the standalone container is attacker-supplied,
and honouring it there would turn "set one header" into a full admin
session — including the root shell proxy. Two conditions, never one:

  * the deployment is the add-on (INGRESS_ONLY), and
  * the request arrived from Supervisor's gateway address.

The gateway check is the same one _ingress_only_middleware already applies
to every request. This module deliberately re-derives it from its own
inputs rather than assuming the middleware ran, because the cost of the
middleware being reordered or bypassed one day is not a 403 — it is silent
authentication bypass.

What this does NOT decide is what the user may then do; role assignment is
the caller's business. See docs and config.yaml's `panel_admin`.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

# Supervisor's gateway address. Every ingress request is proxied from here.
INGRESS_GATEWAY_IP = "172.30.32.2"


class IngressIdentity(NamedTuple):
    """A Home Assistant user, as forwarded by Supervisor."""
    user_id: str
    username: str
    display_name: str


VALID_ROLES = ("admin", "readonly")


def role_for(*, existing_ha_admins: int, configured_default: Optional[str]) -> str:
    """
    The role a newly-seen Home Assistant user is given.

    Admin when nobody can already administer this controller THROUGH INGRESS;
    read-only otherwise.

    The question this asks is "is there already someone who can administer
    this?", and until 2026-08-20 it asked "are there any user rows?" instead.
    Those differ in exactly one case, and it is a case our own migration guide
    walks people into (#235): local password accounts are UNREACHABLE under
    the add-on — the landing page authenticates through ingress before it
    renders any form, and there is deliberately no Sign out — so a local
    admin counted as a user while being an admin nobody could use. Copy a
    container's /data across, as docs/migrate-to-addon.md tells you to so
    your devices keep working, and every Home Assistant user was read-only
    forever, recoverable only by hand-editing the database.

    So this is a corrected count, NOT a loosened rule. The property worth
    protecting is unchanged: the second person through the door is read-only,
    because Home Assistant's ingress view sets requires_auth=False and
    `panel_admin` only hides the sidebar entry, so reaching this dashboard is
    NOT evidence of being trusted with a root shell to every device.
    Promotion is recoverable (PATCH /api/users/{id}); the reverse mistake is
    not recoverable by the person who suffers it.

    A consequence worth stating: if every HA admin is demoted or deleted, the
    next new HA user becomes admin. That is the recovery path working rather
    than a hole — a controller with no reachable admin is the broken state
    this exists to escape, and it is exactly what a fresh install already
    grants the first person through.

    Roles are deliberately NOT mirrored from Home Assistant. Supervisor
    forwards no admin flag, so asking would mean taking `auth_api` — which
    also grants resetting any Home Assistant password, with no verification.
    Too steep for one boolean on a system with one operator. See issue #171.

    An unrecognised configured value falls back to read-only — a typo in a
    config row must never be the thing that grants admin.
    """
    if existing_ha_admins == 0:
        return "admin"
    value = (configured_default or "").strip()
    return value if value in VALID_ROLES else "readonly"


def decide(
    *,
    ingress_only: bool,
    remote: Optional[str],
    user_id: Optional[str],
    username: Optional[str] = None,
    display_name: Optional[str] = None,
) -> Optional[IngressIdentity]:
    """
    Return the Home Assistant identity to authenticate as, or None to fall
    back to ordinary password login.

    None is always the safe answer — it costs a login form, never access.
    """
    # Not the add-on. The header is either absent or attacker-supplied;
    # either way it means nothing here.
    if not ingress_only:
        return None

    # Did not come from Supervisor. Under host_network the container shares
    # the host's netns, so this is the only thing distinguishing a proxied
    # request from one off the LAN.
    if remote != INGRESS_GATEWAY_IP:
        return None

    # Supervisor sets the id only when the ingress session has a user
    # attached. No user means we have not been told who this is, which is
    # not the same as being told it is nobody.
    user_id = (user_id or "").strip()
    if not user_id:
        return None

    # Names are optional in Supervisor's own code — it sets each only if the
    # user record has one. Fall back through to the id, which is always
    # present, so a display name is never required to log in.
    username = (username or "").strip()
    display_name = (display_name or "").strip()

    return IngressIdentity(
        user_id=user_id,
        username=username or display_name or f"ha-{user_id[:12]}",
        display_name=display_name or username or f"ha-{user_id[:12]}",
    )
