"""
Vehicle Mobile Lookup API — Vercel Edition
- SMC API se chassis/engine fetch
- SMC fail ho to backup URL se fetch (recursive search)
- Vahan portal se mobile number extract
- In-memory TTL cache (per-instance, Vercel serverless)
"""

import re, time, threading, json
import warnings
import requests as _reqs
from flask import Flask, request, jsonify

warnings.filterwarnings("ignore", category=_reqs.packages.urllib3.exceptions.InsecureRequestWarning)


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
CONFIG = {
    "sar_client_id":     "3e395e88f29ed880bcace2faec9f2e7b",
    "sar_client_secret": "899f535e4fae7bbbfe2c9eeb0eb0344b",
    "prod_username": "F6SVq3ha/5mIM2Ic5sZ7g/SyzPdAQCHgcHYhpTKbLzuY85LNTkzygfZJWTR+ZP2w",
    "prod_password": "UqOL06PEZ870umUvb6mwp9CpXgh4ZUncnUEADDM/Dm+Y85LNTkzygfZJWTR+ZP2w",
    "stg_username": "8AQuuAynlYZkbgDwIB3rdQTCCdpAMYYr/dB+eKa4moyY85LNTkzygfZJWTR+ZP2w",
    "stg_password": "J7txF8RQWyvfcyQ+rMpltN6t0JnZ48Afqu4mZGJMXXyY85LNTkzygfZJWTR+ZP2w",
    "sig_token_hex":  "54208E16EE9D321F5485A4C256B4EBA2D4044CAAD28255D25578B50C190BD9AB",
    "sarthi_key":     "dlari$#@$G#2033@",
    "permit_key":     "qKzu88J9Y1EgjA1Zsx4RqUhHhBokydd/PVNzEx5RiDY=",
    "npk_sha256":     "1e951ec0e92f22841eb55cfcf22425a161cb7de2eeee1ad5cc42ac315d7ae04c",
    "hash1":          "5b9992f611625ba1a38e0af88a809e62",
    "hash2":          "28a460ce9655446161945ddfd59ead65",
}

CACHE_TTL = None          # None = lifetime (per-instance)
CACHE_MAX_SIZE = 5000


# ═══════════════════════════════════════════════════════════════
# TTL CACHE
# ═══════════════════════════════════════════════════════════════
class TTLCache:
    def __init__(self, ttl=CACHE_TTL, max_size=CACHE_MAX_SIZE, name="cache"):
        self._data = {}
        self._lock = threading.RLock()
        self._ttl = ttl
        self._max = max_size
        self._name = name
        self.hits = 0
        self.misses = 0

    def _now(self):
        return time.time()

    def _expired(self, exp):
        return exp is not None and self._now() > exp

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if not item:
                self.misses += 1
                return None
            value, exp = item
            if self._expired(exp):
                self._data.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key, value, ttl=None):
        with self._lock:
            use_ttl = self._ttl if ttl is None else ttl
            exp = None if use_ttl is None else self._now() + use_ttl
            if len(self._data) >= self._max:
                try:
                    for k in list(self._data.keys()):
                        if self._expired(self._data[k][1]):
                            self._data.pop(k, None)
                    if len(self._data) >= self._max:
                        self._data.pop(next(iter(self._data)))
                except Exception:
                    pass
            self._data[key] = (value, exp)

    def clear(self):
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def stats(self):
        with self._lock:
            total = self.hits + self.misses
            rate = (self.hits / total * 100) if total else 0.0
            return {
                "name": self._name,
                "entries": len(self._data),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(rate, 2),
                "ttl_seconds": self._ttl,
                "max_size": self._max,
            }


_vehicle_cache = TTLCache(ttl=CACHE_TTL, name="vehicle")
_mobile_cache  = TTLCache(ttl=CACHE_TTL, name="mobile")


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
HP = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/statevalidation/homepage.xhtml?statecd=Mzc2MzM2MzAzNjY0MzIzODM3NjIzNjY0MzY2MjM3NDQ0Yw=="
HB = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/statevalidation/homepage.xhtml"
LI = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/usermgmt/login.xhtml"
FR = "https://vahan.parivahan.gov.in/vahanservice/vahan/ui/balanceservice/form_reschedule_fitness.xhtml"

SMC_API_URL = "https://www.smcinsurance.com/central/centralcall/CallReqWithHeader"
BACKUP_API_URL = "https://vehinfoadv.fuckyoubitch.workers.dev/"

TIMEOUT = 6
BACKUP_TIMEOUT = 15

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}
AJAX_HEADERS = {
    "Accept": "application/xml, text/xml, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded",
    "Faces-Request": "partial/ajax",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://vahan.parivahan.gov.in"
}


def build_session():
    sess = _reqs.Session()
    sess.headers.update(BASE_HEADERS)
    sess.verify = False
    adapter = _reqs.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=1)
    sess.mount("https://", adapter)
    return sess


_smc_sess = None
_smc_primed = False


def _get_smc_sess():
    global _smc_sess, _smc_primed
    if _smc_sess is None:
        s = _reqs.Session()
        s.headers.update({"User-Agent": "okhttp/4.9.2"})
        s.verify = False
        adapter = _reqs.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=0)
        s.mount("https://", adapter)
        _smc_sess = s
    if not _smc_primed:
        try:
            _smc_sess.post(SMC_API_URL,
                           json={"url": "GetVaahanDetailsByVehicleNo", "props": ["", "", "0"]},
                           timeout=5)
        except Exception:
            pass
        _smc_primed = True
    return _smc_sess


def _req(sess, url, data=None, headers=None, referer=None):
    hdrs = {}
    if headers: hdrs.update(headers)
    if referer: hdrs["Referer"] = referer
    if data is not None:
        resp = sess.post(url, data=data, headers=hdrs, timeout=TIMEOUT)
    else:
        resp = sess.get(url, headers=hdrs, timeout=TIMEOUT)
    return resp.text, dict(resp.headers)


def _extract_vs(html):
    m = re.search(r'<input[^>]*name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_vs_ajax(html):
    m = re.search(r'<update id="j_id1:javax\.faces\.ViewState:0"><!\[CDATA\[(.*?)\]\]></update>', html)
    return m.group(1) if m else None


# ═══════════════════════════════════════════════════════════════
# VEHICLE FETCH — SMC + Backup
# ═══════════════════════════════════════════════════════════════
def _recursive_find(obj, keys):
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower().replace("_", "").replace("-", "").replace(" ", "")
            for want in keys:
                if want in kl:
                    if isinstance(v, str) and v.strip():
                        return v.strip()
        for v in obj.values():
            found = _recursive_find(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _recursive_find(item, keys)
            if found:
                return found
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                parsed = json.loads(s)
                return _recursive_find(parsed, keys)
            except Exception:
                pass
    return None


def _extract_from_backup_response(raw_text):
    chassis = None
    engine = None

    data = None
    try:
        data = json.loads(raw_text)
    except Exception:
        pass

    if data is not None:
        chassis = _recursive_find(data, ["chassis", "chasis", "chassisno", "chassisnumber",
                                          "chassis_no", "chassis_number", "chassinumber"])
        engine = _recursive_find(data, ["engine", "engineno", "enginenumber",
                                         "engine_no", "engine_number", "enginenumber"])

    if not chassis:
        m = re.search(r'(?:chassis|chasis|chassis\s*no|chassis\s*number)["\s:=]*([A-Z0-9]{10,25})',
                      raw_text, re.IGNORECASE)
        if m:
            chassis = m.group(1)

    if not engine:
        m = re.search(r'(?:engine|engine\s*no|engine\s*number)["\s:=]*([A-Z0-9]{6,25})',
                      raw_text, re.IGNORECASE)
        if m:
            engine = m.group(1)

    if not chassis:
        candidates = re.findall(r'\b[A-HJ-NPR-Z0-9]{17}\b', raw_text)
        if candidates:
            chassis = candidates[0]

    return chassis, engine


def _fetch_vehicle_smc(reg_no):
    sess = _get_smc_sess()
    resp = sess.post(SMC_API_URL,
                     json={"url": "GetVaahanDetailsByVehicleNo", "props": [reg_no, "", "0"]},
                     timeout=10)
    if resp.status_code != 200:
        raise Exception(f"SMC HTTP {resp.status_code}")
    data = resp.json()
    if data.get("statusCode") == 200 and data.get("response"):
        return data["response"]
    raise Exception(f"SMC failed: {data.get('statusMessage', 'unknown')}")


def _fetch_vehicle_backup(reg_no):
    sess = _reqs.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    })
    sess.verify = False
    url = f"{BACKUP_API_URL}?rc={reg_no}"
    resp = sess.get(url, timeout=BACKUP_TIMEOUT)
    if resp.status_code != 200:
        raise Exception(f"Backup HTTP {resp.status_code}")
    raw = resp.text
    chassis, engine = _extract_from_backup_response(raw)
    if not chassis:
        raise Exception("Backup API: chassis not found in response")
    return {"chassis": chassis, "engine": engine or "", "raw": raw}


def _fetch_vehicle_any(reg_no):
    cached = _vehicle_cache.get(reg_no)
    if cached:
        return cached

    errors = []

    try:
        smc = _fetch_vehicle_smc(reg_no)
        chassis = (smc.get("chassis") or "").replace(" ", "").strip()
        engine = (smc.get("engine") or "").strip()
        if chassis and len(chassis) >= 5:
            result = {"chassis": chassis, "engine": engine, "source": "smc"}
            _vehicle_cache.set(reg_no, result)
            return result
        errors.append(f"SMC returned empty/short chassis: '{chassis}'")
    except Exception as e:
        errors.append(f"SMC: {type(e).__name__}: {e}")

    try:
        bk = _fetch_vehicle_backup(reg_no)
        result = {"chassis": bk["chassis"], "engine": bk["engine"], "source": "backup"}
        _vehicle_cache.set(reg_no, result)
        return result
    except Exception as e:
        errors.append(f"Backup: {type(e).__name__}: {e}")

    raise Exception(" | ".join(errors))


# ═══════════════════════════════════════════════════════════════
# MOBILE EXTRACTION — Vahan
# ═══════════════════════════════════════════════════════════════
def get_mobile(reg_no, chassis_no_last5=None):
    start = time.time()

    if chassis_no_last5 is None:
        cached = _mobile_cache.get(reg_no)
        if cached:
            c = dict(cached)
            c["response_time_seconds"] = round(time.time() - start, 2)
            c["cached"] = True
            return c

    result = {
        "success": False,
        "mobile_number": "",
        "chassis_number": "",
        "engine_number": "",
        "chassis_source": "",
        "error": "",
        "response_time_seconds": 0,
        "cached": False,
    }
    try:
        if chassis_no_last5 is None:
            veh = _fetch_vehicle_any(reg_no)
            chassis_full = veh["chassis"]
            engine_no = veh["engine"]
            chassis_no_last5 = chassis_full[-5:]
            result["chassis_number"] = chassis_full
            result["engine_number"] = engine_no
            result["chassis_source"] = veh["source"]
        else:
            result["chassis_source"] = "user"

        sess = build_session()
        result["error"] = ""
        try:
            html, _ = _req(sess, HP)
            vs = _extract_vs(html)
            if not vs:
                raise Exception("No ViewState in homepage")

            cid = re.search(r'<div[^>]*id="(j_idt\d+)"[^>]*class="[^"]*ui-chkbox', html)
            cid = cid.group(1) if cid else "j_idt193"

            ajax_h = dict(AJAX_HEADERS)
            ajax_h["Referer"] = HP

            form = {"javax.faces.partial.ajax": "true",
                    "homepageformid": "homepageformid",
                    "javax.faces.ViewState": vs,
                    "javax.faces.source": "fit_c_office_to",
                    "javax.faces.partial.execute": "fit_c_office_to",
                    "javax.faces.behavior.event": "change",
                    "javax.faces.partial.event": "change",
                    "fit_c_office_to_input": "1"}
            html, _ = _req(sess, HB, data=form, headers=ajax_h)
            vs = _extract_vs_ajax(html) or vs

            form = {"javax.faces.partial.ajax": "true",
                    "homepageformid": "homepageformid",
                    "javax.faces.ViewState": vs,
                    "javax.faces.source": cid,
                    "javax.faces.partial.execute": cid,
                    "javax.faces.partial.render": "proccedHomeButtonId",
                    "javax.faces.behavior.event": "change",
                    f"{cid}_input": "on"}
            html, _ = _req(sess, HB, data=form, headers=ajax_h)
            vs = _extract_vs_ajax(html) or vs

            form = {"javax.faces.partial.ajax": "true",
                    "homepageformid": "homepageformid",
                    "javax.faces.ViewState": vs,
                    "javax.faces.source": "proccedHomeButtonId",
                    "javax.faces.partial.execute": "@all",
                    "proccedHomeButtonId": "proccedHomeButtonId",
                    f"{cid}_input": "on"}
            html, _ = _req(sess, HB, data=form, headers=ajax_h)
            vs = _extract_vs_ajax(html) or vs

            dlg = re.search(r'id="(j_idt\d+)"[^>]*class="[^"]*ui-button', html)
            dlg = dlg.group(1) if dlg else "j_idt536"
            form = {"javax.faces.partial.ajax": "true",
                    "homepageformid": "homepageformid",
                    "javax.faces.ViewState": vs,
                    "javax.faces.source": dlg,
                    "javax.faces.partial.execute": "@all",
                    dlg: dlg,
                    f"{cid}_input": "on"}
            html, _ = _req(sess, HB, data=form, headers=ajax_h)
            vs = _extract_vs_ajax(html) or vs

            html, _ = _req(sess, LI + "?faces-redirect=true", referer=HP)
            vs = _extract_vs(html)
            if not vs:
                raise Exception("Login page: no ViewState")

            fit = re.search(r'id="(j_idt\d+)"[^>]*name="\1"[^>]*type="submit"', html)
            fit = fit.group(1) if fit else "j_idt506"

            html, _ = _req(sess, LI, data={
                "loginForm": "loginForm",
                fit: fit,
                "javax.faces.ViewState": vs,
                "fitbalcTest": "fitbalcTest",
                "pur_cd": "86"
            }, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://vahan.parivahan.gov.in"
            }, referer=LI + "?faces-redirect=true")

            html, _ = _req(sess, FR, referer=LI + "?faces-redirect=true")
            vs = _extract_vs(html)
            if not vs:
                raise Exception("Fitness form: no ViewState")

            ajax_h["Referer"] = FR
            html, _ = _req(sess, FR, data={
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": "balanceFeesFine:validate_dtls",
                "javax.faces.partial.execute": "@all",
                "javax.faces.partial.render": "balanceFeesFine:auth_panel",
                "balanceFeesFine:validate_dtls": "balanceFeesFine:validate_dtls",
                "balanceFeesFine": "balanceFeesFine",
                "balanceFeesFine:tf_reg_no": reg_no,
                "balanceFeesFine:tf_chasis_no": chassis_no_last5,
                "javax.faces.ViewState": vs
            }, headers=ajax_h)

            mobile = None
            for p in [r'id="balanceFeesFine:tf_mobile"[^>]*value="(\d{10})"',
                      r'value="(\d{10})"[^>]*id="balanceFeesFine:tf_mobile"',
                      r'balanceFeesFine:tf_mobile[^>]*value="(\d{10})"']:
                m = re.search(p, html)
                if m and re.match(r'^[6-9]', m.group(1)):
                    mobile = m.group(1)
                    break
            if not mobile:
                nums = re.findall(r'\b[6-9]\d{9}\b', html)
                if nums:
                    mobile = nums[0]

            if mobile:
                result["success"] = True
                result["mobile_number"] = mobile
            else:
                result["error"] = "Mobile number not found in response"

        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    result["response_time_seconds"] = round(time.time() - start, 2)

    if result["success"] and result["chassis_source"] != "user":
        _mobile_cache.set(reg_no, dict(result))

    return result


# ═══════════════════════════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)


def _clean_result(result):
    r = dict(result)
    if not r.get("error"): r.pop("error", None)
    if not r.get("chassis_number"): r.pop("chassis_number", None)
    if not r.get("engine_number"): r.pop("engine_number", None)
    if not r.get("chassis_source"): r.pop("chassis_source", None)
    return r


@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/")
def _index():
    return jsonify({
        "name": "Vehicle Mobile API",
        "version": "2.1-vercel",
        "usage": "GET /api/mobile?vehicle=KL41V3504",
        "optional": "&chassis=00056 (skip auto-fetch)",
        "fallback": "SMC -> backup worker API (recursive chassis search)",
        "cache": {
            "mode": "lifetime (per-instance)" if CACHE_TTL is None else f"ttl={CACHE_TTL}s",
            "stats_endpoint": "/api/cache",
            "clear_endpoint": "DELETE /api/cache",
        },
    })


MOBILE_TIMEOUT = 9   # Vercel free plan = 10s hard limit


def _run_with_timeout(fn, args, timeout):
    out = {"result": {"success": False, "mobile_number": "",
                      "error": "Request timed out",
                      "response_time_seconds": timeout,
                      "cached": False}}
    t = threading.Thread(target=lambda: out.update(result=fn(*args)), daemon=True)
    t.start()
    t.join(timeout)
    return _clean_result(out["result"])


@app.route("/api/mobile", methods=["GET", "OPTIONS"])
def _api_mobile_get():
    if request.method == "OPTIONS":
        return "", 204
    vehicle = request.args.get("vehicle", "").upper().strip()
    chassis = request.args.get("chassis", "").strip() or None
    if not vehicle or len(vehicle) < 6 or len(vehicle) > 12:
        return jsonify({"success": False, "error": "Valid vehicle number required"}), 400
    result = _run_with_timeout(get_mobile, (vehicle, chassis), MOBILE_TIMEOUT)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route("/api/vehicle", methods=["GET"])
def _api_vehicle():
    vehicle = request.args.get("vehicle", "").upper().strip()
    if not vehicle or len(vehicle) < 6 or len(vehicle) > 12:
        return jsonify({"success": False, "error": "Valid vehicle number required"}), 400
    try:
        data = _fetch_vehicle_any(vehicle)
        return jsonify({"success": True, "data": data}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/mobile", methods=["POST"])
def _api_mobile_post():
    data = request.get_json(silent=True) or {}
    vehicle = data.get("vehicle_number", data.get("vehicle", "")).upper().strip()
    chassis = data.get("chassis_last5", data.get("chassis", "")) or None
    if not vehicle or len(vehicle) < 6 or len(vehicle) > 12:
        return jsonify({"success": False, "error": "Valid vehicle_number required"}), 400
    result = _run_with_timeout(get_mobile, (vehicle, chassis), MOBILE_TIMEOUT)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route("/api/cache", methods=["GET"])
def _api_cache_stats():
    return jsonify({
        "vehicle_cache": _vehicle_cache.stats(),
        "mobile_cache": _mobile_cache.stats(),
        "config": {
            "CACHE_TTL": CACHE_TTL,
            "CACHE_MAX_SIZE": CACHE_MAX_SIZE,
            "mode": "lifetime" if CACHE_TTL is None else f"ttl={CACHE_TTL}s",
        },
    }), 200


@app.route("/api/cache", methods=["DELETE", "OPTIONS"])
def _api_cache_clear():
    if request.method == "OPTIONS":
        return "", 204
    _vehicle_cache.clear()
    _mobile_cache.clear()
    return jsonify({"success": True, "message": "Cache cleared"}), 200


# Vercel WSGI handler
handler = app