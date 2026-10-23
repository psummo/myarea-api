#!/usr/bin/env python3
"""Login MyArea F2A + Cartellino (tmapi).

  export MYAREA_USER=... MYAREA_PASS=...
  python3 myarea_tm.py --month 8 --year 2026
  python3 myarea_tm.py --login-only
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import string
import sys
import time
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

CACHE_PATH = os.environ.get(
    "MYAREA_TOKEN_FILE", os.path.expanduser("~/.cache/myarea_tm_token.json")
)

import requests

SSO = "https://sso.myareaf2a.com"
API = "https://api.myareaf2a.com"
TMAPI = "https://tmapi.myareaf2a.com"
LOGIN = "https://login.myareaf2a.com"
WWW = "https://www.myareaf2a.com"
TM = "https://tm.myareaf2a.com"

AUTHORIZE_CLIENT = (
    f"{SSO}/oauth2/authorize-client/"
    "essapplicationKHtxWIP6GD6v8F37FTl7m91fI33WbRkwf4l9rcCp22Q"
)
# Causali supportate: gli ID (justifId) si ricavano da GetAllowedJustifications
# in base al nome, perché cambiano da azienda ad azienda.
CAUSALI = ("SMWO", "ROL", "FERIE")

# Approvatore di fallback (di norma ricavato dall'API). Override: MYAREA_APPROVER
DEFAULT_APPROVER = int(os.environ["MYAREA_APPROVER"]) if os.environ.get("MYAREA_APPROVER") else None
# ID anagrafica: ricavato al login e salvato in cache. Override: MYAREA_ANAG_ID
ANAG_ID = int(os.environ["MYAREA_ANAG_ID"]) if os.environ.get("MYAREA_ANAG_ID") else None

CLIENT_INFO_B64 = (
    "ZXNzYXBwbGljYXRpb25KREpoSkRFd0pFRjRiMDk2VUd4dU1XTldUbW81TjB4bWN5"
    "NXVUQzVqWkcxd00xSlNjMEl4UkZCYVpUSlZSRXB0VjJFdlNrSnpXSEZxUzNKUC5t"
    "eUFyZWFGMkEuY29t"
)


VERBOSE = False


def log(msg: str) -> None:
    if VERBOSE:
        print(msg)


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def b64d(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.b64decode(s + pad).decode()


def force_https(url: str) -> str:
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def follow_https(session: requests.Session, url: str, timeout: int = 45):
    url = force_https(url)
    history = []
    for _ in range(12):
        r = session.get(url, allow_redirects=False, timeout=timeout)
        r.history = list(history)
        loc = r.headers.get("Location")
        log(f"    {r.status_code} {url[:140]}")
        if loc and r.is_redirect:
            url = force_https(urljoin(url, loc))
            history.append(r)
            continue
        return r
    raise RuntimeError("troppi redirect")


def jwt_exp(token: str) -> int | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return int(payload["exp"])
    except Exception:
        return None


def load_cache() -> dict:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(
    jwt: str,
    ess: str | None = None,
    refresh: str | None = None,
    anag_id: Any = None,
) -> None:
    folder = os.path.dirname(CACHE_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    data = {
        "jwt": jwt,
        "exp": jwt_exp(jwt),
        "ess_token": ess,
        "refresh_token": refresh,
        "anag_id": anag_id,
    }
    fd = os.open(CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def cache_jwt_valid(min_ttl: int = 120) -> str | None:
    c = load_cache()
    jwt = c.get("jwt")
    exp = c.get("exp") or (jwt_exp(jwt) if jwt else None)
    if jwt and exp and exp - time.time() > min_ttl:
        left = int(exp - time.time())
        print("[+] JWT da cache, scade tra %d min (%s)" % (left // 60, CACHE_PATH))
        return jwt
    return None


def pick(d: dict, *names: str) -> Any:
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return None


def _find_instances(obj: Any) -> list:
    """Primo elenco di dict con 'idCliente' trovato nel JSON (a qualsiasi livello)."""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) and "idCliente" in x for x in obj):
            return obj
        for x in obj:
            r = _find_instances(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_instances(v)
            if r:
                return r
    return []


def unwrap(obj: Any) -> dict:
    if not isinstance(obj, dict):
        return {}
    for k in ("model", "data", "result", "user", "currentUser"):
        v = obj.get(k)
        if isinstance(v, dict) and any(
            x in v for x in ("user_Name", "userName", "user_name", "name", "company_Anag_Code")
        ):
            return v
    return obj


class MyArea:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": "Mozilla/5.0 MyAreaScript",
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.ess_token: str | None = None
        self.refresh_token: str | None = None
        self.tm_jwt: str | None = None
        self.current_user: dict | None = None
        self.anag_id: Any = ANAG_ID
        self._paused = True
        self._ts_opened = False
        self._justif: dict[str, int] = {}

    def require_anag(self) -> int:
        if not self.anag_id:
            raise RuntimeError(
                "ID anagrafica sconosciuto. Rilancia con --force-login "
                "oppure imposta MYAREA_ANAG_ID."
            )
        return int(self.anag_id)

    def pause(self, lo: float = 0.6, hi: float = 1.4) -> None:
        if self._paused:
            time.sleep(random.uniform(lo, hi))

    def tmapi(self, path: str, payload: dict, module: str = "core") -> dict:
        if not self.tm_jwt:
            raise RuntimeError("manca JWT TM")
        self.pause()
        url = f"{TMAPI}/{module}/v1/api/{path}"
        r = self.s.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.tm_jwt}",
                "Content-Type": "application/json",
                "timezone-offset": "-120",
                "Origin": TM,
                "Referer": f"{TM}/",
            },
            timeout=30,
        )
        if r.status_code == 401:
            raise RuntimeError("JWT scaduto (401). Rilancia con --force-login")
        r.raise_for_status()
        data = r.json()
        if data.get("didError"):
            raise RuntimeError("%s: %s" % (path, data.get("errorMessage") or data))
        return data

    def login(self, user_b64: str, pass_b64: str) -> None:
        log("[+] authorize-client")
        r = follow_https(self.s, AUTHORIZE_CLIENT)
        r.raise_for_status()
        request_id = None
        for hist in list(r.history) + [r]:
            q = parse_qs(urlparse(hist.url).query)
            if "requestId" in q:
                request_id = q["requestId"][0]
            loc = hist.headers.get("Location", "")
            if loc:
                q2 = parse_qs(urlparse(urljoin(hist.url, loc)).query)
                if "requestId" in q2:
                    request_id = q2["requestId"][0]
        if not request_id:
            raise RuntimeError(f"requestId non trovato. URL finale: {r.url}")
        saved = b64(request_id)
        log(f"[+] requestId={request_id[:24]}...")

        self.s.post(
            f"{API}/v1/auth/loginAuth/checkSavedRequest",
            data=saved,
            headers={"Content-Type": "text/plain", "Origin": LOGIN, "Referer": f"{LOGIN}/"},
            timeout=30,
        )

        lr = self.s.post(
            f"{API}/v1/login/ssoLogin",
            json={
                "username": user_b64,
                "password": pass_b64,
                "clientInfo": CLIENT_INFO_B64,
                "savedRequest": saved,
                "mobile": False,
            },
            headers={"Origin": LOGIN, "Referer": f"{LOGIN}/"},
            timeout=30,
        )
        lr.raise_for_status()
        body = lr.json()
        if body.get("twoFactorAuthenticationMethod"):
            raise RuntimeError("Account con 2FA: questo script non la gestisce")
        redir_b64 = body.get("successfullLoginRedirect")
        if not redir_b64:
            raise RuntimeError(f"ssoLogin senza redirect: {body}")
        login_auth = b64d(redir_b64)
        log("[+] loginKey ok")

        log("[+] loginAuthentication + oauth")
        r2 = follow_https(self.s, login_auth)
        sso_data = None
        for hist in list(r2.history) + [r2]:
            q = parse_qs(urlparse(hist.url).query)
            if "ssoData" in q:
                sso_data = q["ssoData"][0]
            loc = hist.headers.get("Location", "")
            if loc:
                q2 = parse_qs(urlparse(urljoin(hist.url, loc)).query)
                if "ssoData" in q2:
                    sso_data = q2["ssoData"][0]
        if not sso_data:
            raise RuntimeError(f"ssoData non trovato. URL={r2.url}")
        log(f"[+] ssoData={sso_data[:24]}...")

        dec = self.s.post(
            f"{API}/v1/ess-fe/sso/decrypt",
            data=sso_data,
            headers={"Content-Type": "text/plain", "Origin": WWW, "Referer": f"{WWW}/"},
            timeout=30,
        )
        dec.raise_for_status()
        payload = dec.json()
        self.ess_token = pick(payload, "access_token", "essToken", "token")
        self.refresh_token = pick(payload, "refresh_token", "refreshToken")
        log("[+] decrypt ok")
        if not self.ess_token:
            raise RuntimeError(f"decrypt senza access_token: {payload}")

        info = self.s.get(
            f"{API}/v1/ess/user/userInfo",
            headers={"Authorization": f"Bearer {self.ess_token}", "Origin": WWW},
            timeout=30,
        )
        info.raise_for_status()
        raw_info = info.json()
        self.current_user = unwrap(raw_info)
        log("[+] userInfo ok")

        # /ess/user (chiamata dall'app TM) aggiunge user_id, type, ecc.
        try:
            full = self.s.get(
                f"{API}/v1/ess/user",
                headers={
                    "Authorization": f"Bearer {self.ess_token}",
                    "Origin": TM,
                    "Referer": f"{TM}/",
                },
                timeout=30,
            )
            full.raise_for_status()
            extra = unwrap(full.json())
            if isinstance(extra, dict):
                self.current_user = {**(self.current_user or {}), **extra}
            log("[+] ess/user ok")
        except Exception as e:
            log("[-] ess/user non disponibile: %s" % e)

        tm_user = self._map_current_user(self.current_user, payload)
        # Istanze (aziende) dell'utente, come fa l'app TM prima di PerformLogin
        instances = self.check_user_instances(tm_user)
        if instances:
            tm_user["selectedClientId"] = instances[0].get("idCliente")
        client_id = tm_user.get("selectedClientId")
        if not client_id:
            raise RuntimeError(
                "ID cliente/azienda non trovato. "
                "Imposta MYAREA_CLIENT_ID (es. il nome dell'azienda come in MyArea)."
            )
        if not instances:
            instances = [
                {
                    "idCliente": client_id,
                    "descrizione": (
                        tm_user.get("selectedClientDescription")
                        or tm_user.get("company_Code")
                        or client_id
                    ),
                    "applicationUrl": None,
                    "blockMessages": [],
                    "alertMessages": [],
                }
            ]
        pl_body = {
            "essToken": self.ess_token,
            "refreshToken": self.refresh_token,
            "essExpire": None,
            "token": None,
            "currentAnagId": None,
            "isAdmin": False,
            "currentUser": tm_user,
            "activeInstances": instances,
            "language": "it",
            "impersonatedUser": None,
            "hasColleagues": False,
            "showTutorial": False,
            "confirmationCode": None,
            "applicationUrl": None,
            "ticketId": None,
            "useSegregation": False,
            "forcedDate": None,
        }
        log("[+] PerformLogin")
        pl = self.s.post(
            f"{TMAPI}/authentication/v1/api/auth/PerformLogin",
            json=pl_body,
            headers={
                "Origin": TM,
                "Referer": f"{TM}/",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        log(f"    status {pl.status_code}")
        try:
            plj = pl.json()
        except Exception:
            print(pl.text[:500])
            pl.raise_for_status()
            return
        if pl.status_code >= 400 or plj.get("didError"):
            print(json.dumps(plj, indent=2, ensure_ascii=False)[:1500])
            raise RuntimeError(
                "PerformLogin fallita. Se è 401, manca lo step logindirect "
                "(ssoData TM). Incolla lo status/body."
            )
        model = plj.get("model") or plj
        self.tm_jwt = pick(model, "token") if isinstance(model, dict) else None
        self.anag_id = (model or {}).get("currentAnagId") if isinstance(model, dict) else None
        if not self.tm_jwt:
            raise RuntimeError(f"PerformLogin senza model.token: {plj}")
        exp = jwt_exp(self.tm_jwt)
        left = int(exp - time.time()) if exp else None
        print("[+] JWT ok anag=%s scade tra %s min" % (self.anag_id, left // 60 if left else "?"))
        save_cache(self.tm_jwt, self.ess_token, self.refresh_token, self.anag_id)

    def check_user_instances(self, tm_user: dict) -> list:
        """checkUserInstancesDirect: restituisce le istanze (idCliente) dell'utente.
        Se la chiamata fallisce o non trova istanze, ritorna [] (si usa il ripiego)."""
        body = {
            "access_token": self.ess_token,
            "refresh_token": self.refresh_token,
            "language_code": tm_user.get("language_Code") or "IT",
            "client_info": "%s_%s" % (
                "".join(random.choices("0123456789abcdef", k=32)),
                "".join(random.choices(string.ascii_letters + string.digits, k=20)),
            ),
            "user_name": tm_user.get("user_Name"),
            "company_anag_code": tm_user.get("company_Anag_Code"),
            "ignoreEmptyCustomers": True,
        }
        try:
            r = self.s.post(
                f"{TMAPI}/authentication/v1/api/auth/checkUserInstancesDirect",
                json=body,
                headers={"Origin": TM, "Referer": f"{TM}/"},
                timeout=30,
            )
            r.raise_for_status()
            resp = r.json()
            # la risposta contiene anche currentUser completo (user_Id, type, ...)
            cu = (resp.get("model") or {}).get("currentUser")
            if isinstance(cu, dict):
                tm_user.update({k: v for k, v in cu.items() if v is not None})
            found = _find_instances(resp)
            log("[+] checkUserInstancesDirect: %d istanze" % len(found))
            return found
        except Exception as e:
            log("[-] checkUserInstancesDirect non disponibile: %s" % e)
            return []

    @staticmethod
    def _map_current_user(info: dict, decrypt: dict) -> dict:
        if not isinstance(info, dict):
            info = {}
        company = pick(info, "company_Anag_Code", "company_anag_code") or decrypt.get(
            "company_anag_code"
        )
        user_name = pick(info, "user_Name", "user_name", "username") or decrypt.get(
            "user_name"
        )
        client = (
            pick(info, "selectedClientId", "clientId", "client_id")
            or pick(decrypt, "selectedClientId", "clientId", "client_id")
            or os.environ.get("MYAREA_CLIENT_ID")
        )
        if not client:
            # userInfo restituisce solo company_code (es. "ACME S.P.A."):
            # come ripiego uso la prima parola, in maiuscolo.
            cc = str(pick(info, "company_Code", "company_code") or "").split()
            client = cc[0].upper() if cc else None
        return {
            "language_Code": pick(info, "language_Code", "language_code") or "IT",
            "company_Id": pick(info, "company_Id", "company_id"),
            "user_Id": pick(info, "user_Id", "user_id"),
            "user_Name": user_name,
            "surname": pick(info, "surname", "family_name"),
            "company_Anag_Code": company,
            "xpers_Key": pick(info, "xpers_Key", "xpers_key"),
            "name": pick(info, "name", "unique_name"),
            "company_Code": pick(info, "company_Code", "company_code"),
            "fiscal_Code": pick(info, "fiscal_Code", "fiscal_code"),
            "email": pick(info, "email"),
            "type": pick(info, "type") or "DP",
            "selectedClientId": client,
            "selectedClientDescription": pick(info, "selectedClientDescription"),
        }

    def month_requests(self, year: int, month: int) -> Any:
        return self.tmapi(
            "dashboard/GetMonthRequests",
            {"isLoan": False, "selectedPersonId": None, "year": year, "month": month},
        )

    def flatten_requests(self, data: dict) -> list:
        m = (data or {}).get("model") or {}
        out = []
        for stato in ("waiting", "approved", "rejected", "suspended", "revoked"):
            for r in m.get(stato) or []:
                j = r.get("justification") or {}
                t0 = _fmt_time(j.get("tooltipFromTime") or j.get("gridFromTime"))
                t1 = _fmt_time(j.get("tooltipToTime") or j.get("gridToTime"))
                out.append({
                    "idRequest": r.get("idRequest"),
                    "stato": stato,
                    "causale": (j.get("code") or j.get("name") or "").upper(),
                    "dal": (j.get("fromDate") or "")[:10],
                    "al": (j.get("toDate") or j.get("fromDate") or "")[:10],
                    "dalle": t0,
                    "alle": t1,
                    "intera": not t0,
                })
        revoked_ids = {x["idRequest"] for x in out if x["stato"] == "revoked" and x.get("idRequest")}
        revoked_sig = {
            (x["causale"], x["dal"], x["al"], x.get("dalle"), x.get("alle"))
            for x in out if x["stato"] == "revoked"
        }
        cleaned = []
        for x in out:
            if x["stato"] == "approved" and (
                x.get("idRequest") in revoked_ids
                or (x["causale"], x["dal"], x["al"], x.get("dalle"), x.get("alle")) in revoked_sig
            ):
                continue
            cleaned.append(x)
        return cleaned

    def same_request(self, item: dict, causale: str, day_iso: str, dalle=None, alle=None) -> bool:
        if item.get("causale") != causale.upper():
            return False
        if not (item.get("dal") <= day_iso <= (item.get("al") or item.get("dal"))):
            return False
        if not dalle and not alle:
            return bool(item.get("intera") or not item.get("dalle"))
        return item.get("dalle") == dalle and item.get("alle") == alle

    def drop_requests(self, items: list) -> None:
        for t in items:
            if t["stato"] not in ("waiting", "approved"):
                continue
            print(" - drop", t["causale"], t["stato"], t.get("dalle") or "intera", t["idRequest"])
            if t["stato"] == "waiting":
                self.delete_request(t["idRequest"])
            else:
                self.revoke_request(t["idRequest"])

    def covering(self, items: list, day_iso: str, causale: str | None) -> list:
        out = []
        want = (causale or "").upper() or None
        for it in items:
            if not it.get("dal"):
                continue
            if it["dal"] <= day_iso <= (it.get("al") or it["dal"]):
                if want is None or it["causale"] == want:
                    out.append(it)
        return out

    def extract_order(self, detail: dict) -> dict:
        model = detail.get("model") or detail
        orders = model.get("orders") or []
        if not orders and isinstance(model, list):
            orders = model
        if not orders:
            raise RuntimeError("Nessuna commessa (orders vuoto)")
        order = orders[0]
        steps = order.get("steps") or [{}]
        step = steps[0]
        ht = None
        for h in step.get("hourTypes") or []:
            if h.get("id") == 2 or "ORE_LAVOR" in str(h.get("code") or ""):
                ht = h
                break
        return {
            "orderId": order.get("id") or step.get("orderId"),
            "stepId": step.get("id"),
            "nodeId": step.get("nodeId"),
            "hourTypeId": (ht or {}).get("id") or 2,
            "code": order.get("code") or step.get("code"),
            "name": order.get("name"),
        }

    def allowed_justifications(self, day_iso: str) -> list:
        data = self.tmapi(
            "daily/GetAllowedJustifications",
            {
                "justType": None,
                "isLoan": False,
                "selectedPersonId": None,
                "date": day_iso + "T00:00:00.000Z",
            },
        )
        return data.get("model") or []

    def justif_id(self, causale: str, day_iso: str) -> int:
        """justifId della causale (SMWO, ROL, FERIE), ricavato dalle causali
        consentite all'utente (campo name/itpCode di GetAllowedJustifications)."""
        key = causale.upper()
        if key not in self._justif:
            for j in self.allowed_justifications(day_iso):
                for field in ("name", "itpCode"):
                    val = str(j.get(field) or "").strip().upper()
                    if val and val not in self._justif:
                        self._justif[val] = int(j["justifId"])
        if key not in self._justif:
            raise RuntimeError(
                "Causale %s non disponibile per questo utente (causali: %s)"
                % (key, ", ".join(sorted(self._justif)) or "nessuna")
            )
        return self._justif[key]

    def justification_details(self, justif_id: int, day_iso: str) -> dict:
        return self.tmapi(
            "daily/GetJustificationDetails",
            {
                "justifId": justif_id,
                "selectedDay": day_iso + "T00:00:00.000Z",
                "selectedPersonId": None,
                "isLoan": False,
                "forSplit": False,
            },
        )

    def add_justification(
        self,
        causale: str,
        day_iso: str,
        approver: int | None = DEFAULT_APPROVER,
        dalle: str | None = None,
        alle: str | None = None,
    ) -> dict:
        jid = self.justif_id(causale, day_iso)
        intera = not (dalle and alle)
        body = {
            "justifId": jid,
            "idApprover": approver,
            "selectedPersonId": None,
            "isLoan": False,
            "selectedDay": day_iso,
            "timeSlot": 1 if intera else 0,
            "fromDay": day_iso,
            "toDay": day_iso,
            "fromTime": None if intera else "2000-01-31T%s:00.000Z" % _hhmm_to_utc(dalle),
            "toTime": None if intera else "2000-01-31T%s:00.000Z" % _hhmm_to_utc(alle),
        }
        print("[+] AddJustification", causale, day_iso, "intera" if intera else "%s-%s" % (dalle, alle))
        return self.tmapi("managerequests/AddJustification", body)

    def delete_request(self, id_request: int) -> dict:
        print("[+] DeleteRequest", id_request)
        return self.tmapi(
            "managerequests/DeleteRequest",
            {"idRequest": id_request, "selectedPersonId": None, "isLoan": False},
        )

    def revoke_request(self, id_request: int) -> dict:
        print("[+] RevokeRequest", id_request)
        return self.tmapi(
            "managerequests/RevokeRequest",
            {"idRequest": id_request, "selectedPersonId": None, "isLoan": False},
        )

    def save_visit(self) -> None:
        if self._ts_opened:
            return
        self.tmapi("log/SaveVisit", {"pageName": "ts-details"})
        self._ts_opened = True

    def timesheet_id(self, year: int, month: int) -> int:
        # timeSheetDate = fine mese precedente in UTC (offset -120)
        import datetime as dt
        first = dt.datetime(year, month, 1)
        prev = first - dt.timedelta(days=1)
        stamp = "%sT23:00:00.000Z" % prev.strftime("%Y-%m-%d")
        data = self.tmapi(
            "timeSheet/GetTimeSheetId",
            {"idAnag": self.require_anag(), "timeSheetDate": stamp},
            module="timesheet",
        )
        return int(data["model"])

    def timesheet_summary(self, year: int) -> dict:
        return self.tmapi(
            "timeSheet/GetTimeSheetSummary",
            {"idAnag": self.require_anag(), "year": year, "showSquaring": False},
            module="timesheet",
        )

    def timesheet_row(self, year: int, month: int) -> dict:
        model = (self.timesheet_summary(year).get("model") or {})
        for row in model.get("timeSheets") or []:
            if int(row.get("monthNumber") or 0) == month:
                return row
        return {}

    def timesheet_chiuso(self, year: int, month: int) -> tuple[bool, dict]:
        row = self.timesheet_row(year, month)
        status = row.get("status")
        tsid = int(row.get("timeSheetId") or 0)
        chiuso = status == 2 or tsid == 0 or status == -1
        return chiuso, row

    def assert_timesheet_aperto(self, year: int, month: int) -> dict:
        chiuso, row = self.timesheet_chiuso(year, month)
        if chiuso:
            st = row.get("status")
            raise RuntimeError(
                "Timesheet %02d/%s non modificabile (status=%s, id=%s). "
                "Solo elenco/quadratura, oppure riapri il foglio."
                % (month, year, st, row.get("timeSheetId"))
            )
        return row

    def check_timesheet(self, tsid: int, closing: bool = True) -> dict:
        return self.tmapi(
            "timeSheet/CheckTimeSheet",
            {"closing": closing, "timesheetId": tsid},
            module="timesheet",
        )

    def timesheet_operation(self, tsid: int, operation: int) -> dict:
        return self.tmapi(
            "timeSheet/TimesheetOperation",
            {
                "timesheetIds": [int(tsid)],
                "operation": operation,
                "approverId": None,
                "closedTo": None,
            },
            module="timesheet",
        )

    def chiudi_timesheet(self, year: int, month: int) -> None:
        row = self.timesheet_row(year, month)
        tsid = int(row.get("timeSheetId") or 0)
        if not tsid:
            raise RuntimeError("Nessun timesheet per %02d/%s" % (month, year))
        if row.get("status") == 2:
            print("[+] timesheet %02d/%s già chiuso (id=%s)" % (month, year, tsid))
            return
        waiting = (self.month_requests(year, month).get("model") or {}).get("waiting") or []
        if waiting:
            raise RuntimeError(
                "Non chiudo %02d/%s: %d richieste in attesa"
                % (month, year, len(waiting))
            )
        chk = self.check_timesheet(tsid, closing=True)
        model = chk.get("model") or {}
        infos = model.get("checkinfos") or {}
        if infos.get("error") or model.get("rc") not in (None, "ok"):
            raise RuntimeError("CheckTimeSheet ko: %s" % (infos.get("errormsgs") or model))
        print("[+] CheckTimeSheet ok, chiudo", tsid)
        out = self.timesheet_operation(tsid, 0)
        print("[+] TimesheetOperation", (out.get("model") or out))

    def previous_month_orders(self, tsid: int) -> list:
        data = self.tmapi(
            "timeSheet/GetPreviousMonthOrders",
            {"timesheetId": int(tsid), "day": None},
            module="timesheet",
        )
        return data.get("model") or []

    def apri_timesheet(self, year: int, month: int) -> dict:
        row = self.timesheet_row(year, month)
        tsid = int(row.get("timeSheetId") or 0)
        if not tsid:
            raise RuntimeError("Nessun timesheet per %02d/%s" % (month, year))
        self.save_visit()
        if row.get("status") != 2:
            print("[+] timesheet %02d/%s già aperto (id=%s, status=%s)" % (month, year, tsid, row.get("status")))
            return row
        out = self.timesheet_operation(tsid, 1)
        print("[+] TimesheetOperation open", (out.get("model") or out))
        return out

    def giorni_sheet(self, detail: dict) -> list:
        model = detail.get("model") or detail
        out = []
        for d in model.get("days") or []:
            iso = str(d.get("date") or d.get("day") or "")[:10]
            if not iso:
                continue
            out.append({
                "iso": iso,
                "rest": bool(d.get("isRest")),
                "holiday": bool(d.get("isHoliday")),
                "vacancy": bool(d.get("isVacancy")),
            })
        return out

    def init_mese(self, year: int, month: int) -> None:
        now = datetime.now()
        if (year, month) != (now.year, now.month):
            raise RuntimeError(
                "Init solo sul mese attuale (%02d/%s), non %02d/%s"
                % (now.month, now.year, month, year)
            )
        row = self.timesheet_row(year, month)
        tsid = int(row.get("timeSheetId") or 0)
        if not tsid:
            tsid = self.timesheet_id(year, month)
        if not tsid:
            raise RuntimeError("Mese %02d/%s non esiste ancora (id=0)." % (month, year))
        status = row.get("status")
        self.save_visit()
        if status == 2:
            raise RuntimeError(
                "Timesheet %02d/%s chiuso (id=%s). Non inizializzo un foglio chiuso."
                % (month, year, tsid)
            )
        print("[+] foglio aperto id=%s status=%s, niente TimesheetOperation" % (tsid, status))
        tsid, detail = self.open_timesheet(year, month)
        orders = (detail.get("model") or {}).get("orders") or []
        if not orders:
            prev = self.previous_month_orders(tsid)
            if not prev:
                raise RuntimeError("GetPreviousMonthOrders vuoto")
            order = self.extract_order({"orders": prev})
            print("[+] commessa da mese precedente", order.get("code"), order.get("name"), order.get("orderId"))
        else:
            order = self.extract_order(detail)
            print("[+] commessa già sul foglio", order.get("code") or order.get("orderId"))
        giorni = self.giorni_sheet(detail)
        if not giorni:
            import calendar
            giorni = []
            for d in range(1, calendar.monthrange(year, month)[1] + 1):
                iso = "%04d-%02d-%02d" % (year, month, d)
                wd = datetime(year, month, d).weekday()
                giorni.append({"iso": iso, "rest": wd >= 5, "holiday": False, "vacancy": False})
        n = skip = 0
        for g in giorni:
            if g["rest"] or g["holiday"] or g["vacancy"]:
                print("SKIP", g["iso"], "rosso")
                skip += 1
                continue
            self.set_hours_if_needed(tsid, detail, g["iso"], 8, order)
            n += 1
        print("[+] init %02d/%s 8h su %d giorni, skip %d" % (month, year, n, skip))

    def open_timesheet(self, year: int, month: int) -> tuple[int, dict]:
        tsid = self.timesheet_id(year, month)
        detail = self.tmapi(
            "timeSheet/GetTimeSheetDetail",
            {"timesheetId": tsid, "day": None},
            module="timesheet",
        )
        return tsid, detail

    def hours_for_day(self, detail: dict, day_iso: str) -> float | None:
        model = detail.get("model") or detail
        prefix = day_iso
        for order in model.get("orders") or []:
            for step in order.get("steps") or []:
                for ht in step.get("hourTypes") or []:
                    if ht.get("id") not in (2, None) and ht.get("code") and "ORE_LAVOR" not in str(ht.get("code")):
                        continue
                    for d in ht.get("days") or []:
                        if str(d.get("day") or "").startswith(prefix):
                            val = d.get("numericValue")
                            if val is None and d.get("value"):
                                val = float(str(d.get("value")).replace(",", "."))
                            return float(val or 0)
        return None

    def set_hours_if_needed(self, tsid: int, detail: dict, day_iso: str, hours: float, order: dict) -> None:
        cur = self.hours_for_day(detail, day_iso)
        if cur is not None and abs(cur - hours) < 0.01:
            print("[+] timesheet %s già %s, skip" % (day_iso, cur))
            return
        print("[+] timesheet %s %s -> %s" % (day_iso, cur, hours))
        self.add_hour(tsid, day_iso, hours, order)

    def add_hour(self, tsid: int, day_iso: str, hours: float, order: dict) -> dict:
        print("[+] AddTimeSheetHour", day_iso, hours)
        return self.tmapi(
            "timeSheet/AddTimeSheetHour",
            {
                "day": day_iso + "T00:00:00",
                "hourTypeId": order.get("hourTypeId", 2),
                "notes": None,
                "orderId": order["orderId"],
                "stepId": order["stepId"],
                "nodeId": order["nodeId"],
                "timesheetId": tsid,
                "value": hours,
                "planned": False,
                "dayOnly": False,
            },
            module="timesheet",
        )


def _fmt_time(val) -> str | None:
    if not val:
        return None
    s = str(val)
    if "T" in s:
        s = s.split("T", 1)[1]
    s = s.replace("Z", "")[:5]
    if len(s) >= 5 and s[2] == ":":
        return s[:5]
    return None


def _hhmm_to_utc(hhmm: str) -> str:
    """MyArea mostra fromTime cosi com'e'. Non sottrarre il fuso."""
    h, m = (int(x) for x in hhmm.split(":"))
    return "%02d:%02d" % (h, m)


NOMI_GIORNO = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


def label_giorno(iso: str) -> str:
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d")
    except ValueError:
        return iso or ""
    return "%d %s" % (d.day, NOMI_GIORNO[d.weekday()])


def parse_periodi(spec: Optional[str]) -> List[Tuple[str, str]]:
    oggi = datetime.now()
    if not spec:
        iso = oggi.strftime("%Y-%m-%d")
        return [(iso, iso)]
    year, month = oggi.year, oggi.month
    periodi = []
    for raw in spec.split(","):
        part = raw.strip()
        if not part:
            continue
        import re
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{1,2})-(\d{1,2})", part)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            a, b = int(m.group(3)), int(m.group(4))
            periodi.append((
                "%04d-%02d-%02d" % (year, month, a),
                "%04d-%02d-%02d" % (year, month, b),
            ))
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            year, month, _day = (int(x) for x in part.split("-"))
            periodi.append((part, part))
            continue
        if re.fullmatch(r"\d{1,2}-\d{1,2}", part):
            a, b = (int(x) for x in part.split("-"))
            periodi.append((
                "%04d-%02d-%02d" % (year, month, a),
                "%04d-%02d-%02d" % (year, month, b),
            ))
            continue
        if re.fullmatch(r"\d{1,2}", part):
            periodi.append((
                "%04d-%02d-%02d" % (year, month, int(part)),
                "%04d-%02d-%02d" % (year, month, int(part)),
            ))
            continue
        raise ValueError("Data non valida: %s" % part)
    return periodi


def giorni_lavorativi(inizio: str, fine: str) -> List[str]:
    cur = datetime.strptime(inizio, "%Y-%m-%d")
    end = datetime.strptime(fine, "%Y-%m-%d")
    out = []
    nomi = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        else:
            print("SKIP %s" % label_giorno(cur.strftime("%Y-%m-%d")))
        cur += timedelta(days=1)
    return out


def giorni_da_spec(spec: str) -> List[str]:
    giorni = []
    for a, b in parse_periodi(spec):
        giorni.extend(giorni_lavorativi(a, b))
    oggi = datetime.now()
    pref = "%04d-%02d-" % (oggi.year, oggi.month)
    fuori = [g for g in giorni if not g.startswith(pref)]
    if fuori:
        raise ValueError(
            "Si opera solo nel mese corrente (%s). Fuori: %s"
            % (pref.strip("-"), ", ".join(fuori))
        )
    return giorni


def print_summary(data: dict) -> None:
    m = (data or {}).get("model") or {}
    if data.get("didError"):
        print("ERROR", data.get("errorMessage"))
        return
    rev_ids = {r.get("idRequest") for r in (m.get("revoked") or []) if r.get("idRequest")}
    for stato in ("waiting", "approved", "rejected", "suspended", "revoked"):
        arr = m.get(stato) or []
        if stato == "approved":
            arr = [r for r in arr if r.get("idRequest") not in rev_ids]
        print("=== %s (%d) ===" % (stato, len(arr)))
        for r in arr:
            j = r.get("justification") or {}
            t0, t1 = j.get("tooltipFromTime"), j.get("tooltipToTime")
            fascia = ""
            if t0:
                s0, s1 = str(t0), str(t1 or "")
                fascia = "%s-%s" % (s0[11:16] if "T" in s0 else s0[:5], s1[11:16] if "T" in s1 else s1[:5])
            fd = (j.get("fromDate") or "")[:10]
            td = (j.get("toDate") or "")[:10]
            gd, gt = fd[8:10], td[8:10]
            arco = gd if gd == gt else "%s->%s" % (gd, gt)
            if gd == gt:
                quando = label_giorno(fd)
            else:
                quando = "%s → %s" % (label_giorno(fd), label_giorno(td))
            extra = (" " + fascia) if fascia else ""
            print("%s %s%s" % (j.get("code"), quando, extra))


def print_quadratura(detail: dict) -> None:
    model = detail.get("model") or detail
    checks = model.get("checks") or []
    q = None
    for c in checks:
        name = str(c.get("name") or "")
        if c.get("id") == 1 or "QUADRATURA" in name.upper():
            q = c
            break
    print("Incongruenze")
    n = 0
    if not q:
        print("* nessuna riga QUADRATURA ORE nel detail")
        return
    for d in q.get("days") or []:
        day = (d.get("day") or "")[:10]
        err = d.get("errors") or []
        warn = d.get("warnings") or []
        dich = d.get("orderHours")
        att = d.get("presenceHours")
        bad = bool(err) or (dich is not None and att is not None and str(dich) != str(att))
        if not bad:
            continue
        n += 1
        extra = ""
        if err:
            extra = " (%s)" % "; ".join(str(x) for x in err)
        print("* %s: dichiarato %s / atteso %s%s" % (label_giorno(day), dich, att, extra))
    if n == 0:
        print("* nessuna")


def _minuti(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def _hhmm(total: int) -> str:
    return "%02d:%02d" % (total // 60, total % 60)


def fasce_intorno(buchi: list, inizio: str, fine: str) -> list:
    """buchi = [(dalle, alle), ...] già occupati da ROL. Ritorna fasce SMWO."""
    t0, t1 = _minuti(inizio), _minuti(fine)
    slots = sorted((_minuti(a), _minuti(b)) for a, b in buchi if a and b)
    cur = t0
    out = []
    for a, b in slots:
        if a > cur:
            out.append((_hhmm(cur), _hhmm(a)))
        cur = max(cur, b)
    if cur < t1:
        out.append((_hhmm(cur), _hhmm(t1)))
    return out


HELP = """
Esempi (mese = quello di oggi; date fuori mese → errore)

  export MYAREA_USER=tuo_username MYAREA_PASS='...'

  # elenco richieste (default: mese corrente)
  python3 myarea_tm.py
  python3 myarea_tm.py --json

  # solo login / rinnovo JWT
  python3 myarea_tm.py --login-only
  python3 myarea_tm.py --force-login --login-only

  # SMWO giornata intera (se c'è già un ROL a fascia, lo SMWO viene
  # spezzato prima/dopo il ROL, non sovrascrive il ROL)
  # Se la data non è specificata, la richiesta (ROL-FERIE-SMWO) è per la giornata corrente
  python3 myarea_tm.py --crea --causale SMWO
  python3 myarea_tm.py --crea --data 22 --causale SMWO
  python3 myarea_tm.py --crea --data 22,24-26 --causale SMWO

  # ROL fascia: cancella SMWO quel giorno e lo ricrea intorno al ROL
  python3 myarea_tm.py --crea --data 22 --causale ROL --dalle 10:30 --alle 12:30
  python3 myarea_tm.py --crea --data 22 --causale ROL --dalle 10:30 --alle 12:30 --inizio 09:00 --fine 18:00

  # FERIE: toglie tutto quel giorno, timesheet 0
  python3 myarea_tm.py --crea --data 22 --causale FERIE
  python3 myarea_tm.py --crea --data 22-24 --causale FERIE

  # cancella/revoca (default: solo approvate)
  python3 myarea_tm.py --cancella --data 22 --causale SMWO
  python3 myarea_tm.py --cancella --data 22 --causale ROL --ancheInAttesa
  python3 myarea_tm.py --cancella --data 22 --ancheInAttesa

  # quadratura timesheet (ok anche se chiuso)
  python3 myarea_tm.py --quadratura

  # chiudi il timesheet del mese corrente (dopo CheckTimeSheet)
  python3 myarea_tm.py --chiudi
  python3 myarea_tm.py --apri
  python3 myarea_tm.py --init-mese
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="MyArea Cartellino via API (niente browser).",
        epilog=HELP,
    )
    ap.add_argument("--year", type=int, default=None, help="default: anno corrente")
    ap.add_argument("--month", type=int, default=None, help="default: mese corrente")
    ap.add_argument("--login-only", action="store_true")
    ap.add_argument("--json", action="store_true", help="stampa il JSON grezzo")
    ap.add_argument("--force-login", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="log SSO/login")
    ap.add_argument("--cancella", action="store_true")
    ap.add_argument("--crea", action="store_true")
    ap.add_argument("--quadratura", action="store_true")
    ap.add_argument("--chiudi", action="store_true", help="chiude il timesheet del mese")
    ap.add_argument("--apri", action="store_true", help="riapre il timesheet (operation 1)")
    ap.add_argument("--init-mese", action="store_true", help="apre, commessa precedente, 8h sui giorni lavorativi")
    ap.add_argument("--data", default=None, help="giorno mese corrente: 22 oppure 22-24,18. Se manca, oggi")
    ap.add_argument("--causale", default=None, help="ROL, SMWO, FERIE")
    ap.add_argument("--dalle", default=None)
    ap.add_argument("--alle", default=None)
    ap.add_argument("--inizio", default="09:00")
    ap.add_argument("--fine", default="18:00")
    ap.add_argument(
        "--ancheInAttesa",
        action="store_true",
        help="in --cancella include anche le in attesa (default: solo approvate)",
    )
    args = ap.parse_args()
    global VERBOSE
    VERBOSE = bool(args.verbose)
    now = datetime.now()
    if args.year is None:
        args.year = now.year
    if args.month is None:
        args.month = now.month

    user_b64 = os.environ.get("MYAREA_USER_B64")
    pass_b64 = os.environ.get("MYAREA_PASS_B64")
    if not user_b64 and os.environ.get("MYAREA_USER"):
        user_b64 = b64(os.environ["MYAREA_USER"])
    if not pass_b64 and os.environ.get("MYAREA_PASS"):
        pass_b64 = b64(os.environ["MYAREA_PASS"])
    if not user_b64 or not pass_b64:
        print("Imposta MYAREA_USER+MYAREA_PASS oppure *_B64", file=sys.stderr)
        return 1

    client = MyArea()
    cached = None if args.force_login else cache_jwt_valid()
    if cached:
        client.tm_jwt = cached
        client.anag_id = load_cache().get("anag_id") or ANAG_ID
    else:
        client.login(user_b64, pass_b64)
    if args.login_only:
        print("[+] login-only: JWT ottenuto" if client.tm_jwt else "[-] niente JWT")
        return 0
    if args.quadratura:
        tsid, detail = client.open_timesheet(args.year, args.month)
        print("[+] timesheetId", tsid)
        print_quadratura(detail)
        print("=== GetMonthAnomalies ===")
        try:
            an = client.tmapi(
                "dashboard/GetMonthAnomalies",
                {
                    "isLoan": False,
                    "selectedPersonId": client.require_anag(),
                    "year": args.year,
                    "month": args.month,
                },
            )
            print(json.dumps(an.get("model"), indent=2, ensure_ascii=False)[:2000])
        except Exception as e:
            print("anomalies:", e)
        return 0

    if args.apri:
        try:
            client.apri_timesheet(args.year, args.month)
        except Exception as e:
            print("ERRORE", e, file=sys.stderr)
            return 1
        return 0

    if args.init_mese:
        try:
            client.init_mese(args.year, args.month)
        except Exception as e:
            print("ERRORE", e, file=sys.stderr)
            return 1
        return 0

    if args.chiudi:
        try:
            client.chiudi_timesheet(args.year, args.month)
        except Exception as e:
            print("ERRORE", e, file=sys.stderr)
            return 1
        return 0

    if (args.crea or args.cancella) and not args.data:
        args.data = str(datetime.now().day)
        print("[+] data omessa, uso oggi", args.data)

    if args.cancella:
        giorni = giorni_da_spec(args.data)
        mesi = {}
        ts_cache = {}
        for day in giorni:
            year, month, _ = (int(x) for x in day.split("-"))
            key = (year, month)
            if key not in mesi:
                client.assert_timesheet_aperto(year, month)
                mesi[key] = client.flatten_requests(client.month_requests(year, month))
            items = mesi[key]
            targets = client.covering(items, day, args.causale)
            if args.ancheInAttesa:
                targets = [t for t in targets if t["stato"] in ("waiting", "approved")]
            else:
                skipped = [t for t in targets if t["stato"] == "waiting"]
                targets = [t for t in targets if t["stato"] == "approved"]
                for t in skipped:
                    print("SKIP in attesa", t["causale"], t["idRequest"], "(usa --ancheInAttesa)")
            print(day, "da cancellare:", len(targets))
            for t in targets:
                print(" -", t["causale"], t["stato"], t["dal"], t["al"], t["idRequest"])
                if t["stato"] == "waiting":
                    client.delete_request(t["idRequest"])
                else:
                    client.revoke_request(t["idRequest"])
            if key not in ts_cache:
                ts_cache[key] = client.open_timesheet(year, month)
            tsid, detail = ts_cache[key]
            order = client.extract_order(detail)
            client.set_hours_if_needed(tsid, detail, day, 8, order)
        return 0

    if args.crea:
        if not args.causale:
            print("Serve --causale SMWO|ROL|FERIE", file=sys.stderr)
            return 1
        causale = args.causale.upper()
        for day in giorni_da_spec(args.data):
            year, month, _ = (int(x) for x in day.split("-"))
            client.assert_timesheet_aperto(year, month)
            data = client.month_requests(year, month)
            attivi = [t for t in client.flatten_requests(data) if t["stato"] in ("waiting", "approved")]

            def _approver(cau):
                det = client.justification_details(client.justif_id(cau, day), day)
                apps = ((det.get("model") or {}).get("approvers") or [])
                if apps:
                    return apps[0].get("idApprover")
                if DEFAULT_APPROVER is None:
                    raise RuntimeError(
                        "Nessun approvatore trovato per %s: imposta MYAREA_APPROVER" % cau
                    )
                return DEFAULT_APPROVER
    
            def _minuti(hhmm):
                h, m = (int(x) for x in hhmm.split(":"))
                return h * 60 + m
    
            def ensure(cau, dalle=None, alle=None):
                stessi = [t for t in attivi if client.same_request(t, cau, day, dalle, alle)]
                if stessi:
                    print("SKIP", cau, day, dalle or "intera", "già identica")
                    return
                altri = [
                    t for t in client.covering(attivi, day, cau)
                    if not client.same_request(t, cau, day, dalle, alle)
                ]
                if altri:
                    print("Orario diverso, rifaccio", cau)
                    client.drop_requests(altri)
                client.add_justification(cau, day, approver=_approver(cau), dalle=dalle, alle=alle)
    
            wrap_rol = causale == "ROL" and args.dalle and args.alle
            if causale == "FERIE":
                client.drop_requests(client.covering(attivi, day, None))
                client.add_justification("FERIE", day, approver=_approver("FERIE"))
                ore = 0
            elif wrap_rol:
                smwo = client.covering(attivi, day, "SMWO")
                client.drop_requests(smwo)
                ensure("ROL", args.dalle, args.alle)
                fasce = fasce_intorno([(args.dalle, args.alle)], args.inizio, args.fine)
                for a, b in fasce:
                    client.add_justification("SMWO", day, approver=_approver("SMWO"), dalle=a, alle=b)
                from math import ceil
                ore = max(8 - int(ceil((_minuti(args.alle) - _minuti(args.dalle)) / 60.0)), 0)
            elif causale == "SMWO" and not (args.dalle and args.alle):
                rols = [
                    (t.get("dalle"), t.get("alle"))
                    for t in client.covering(attivi, day, "ROL")
                    if t.get("dalle") and t.get("alle")
                ]
                rols_intere = [
                    t for t in client.covering(attivi, day, "ROL") if t.get("intera")
                ]
                client.drop_requests(client.covering(attivi, day, "SMWO"))
                if rols_intere:
                    print("SKIP SMWO", day, "ROL giornata intera già presente")
                    ore = 0
                elif rols:
                    fasce = fasce_intorno(rols, args.inizio, args.fine)
                    print("SMWO wrap intorno a ROL", rols, "->", fasce)
                    for a, b in fasce:
                        client.add_justification("SMWO", day, approver=_approver("SMWO"), dalle=a, alle=b)
                    from math import ceil
                    rol_min = sum(_minuti(b) - _minuti(a) for a, b in rols)
                    ore = max(8 - int(ceil(rol_min / 60.0)), 0)
                else:
                    ensure("SMWO")
                    ore = 8
            else:
                ensure(causale, args.dalle, args.alle)
                ore = 0 if causale == "FERIE" else 8
    
            tsid, detail = client.open_timesheet(year, month)
            order = client.extract_order(detail)
            client.set_hours_if_needed(tsid, detail, day, ore, order)
            print("[+] fatto", causale, day, "timesheet target", ore)
        return 0

    data = client.month_requests(args.year, args.month)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_summary(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
