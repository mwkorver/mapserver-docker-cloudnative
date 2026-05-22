#!/usr/bin/env python3
"""
Small S3 SigV4 signing proxy for nginx proxy_cache misses.

GDAL reads /vsicurl/http://localhost:8001/<key>. nginx owns the range-aware
cache on port 8001, then forwards cache misses here. This process signs the
origin request with task/container IAM credentials and streams the S3 response
back to nginx.
"""

import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen


LISTEN_HOST = os.environ.get("S3_SIGNER_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("S3_SIGNER_LISTEN_PORT", "9000"))
S3_BUCKET = os.environ.get("S3_BUCKET", "kyfromabove")
S3_REGION = os.environ.get("S3_REGION", "us-west-2")
SIGNING_MODE = os.environ.get("S3_SIGNING", "auto").lower()
SERVICE = "s3"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
METADATA_TIMEOUT = float(os.environ.get("AWS_METADATA_TIMEOUT", "1.0"))
ENABLE_EC2_METADATA = os.environ.get("S3_SIGNER_EC2_METADATA", "false").lower() in {"1", "true", "yes"}

# Buckets that must be accessed WITHOUT signing (public buckets in other
# accounts whose bucket policies deny authenticated cross-account requests).
# Comma-separated list; overridable via env.  njogis-imagery is included by
# default because it is a public NJ state bucket that returns 403 for any
# signed request originating from a different AWS account.
_PUBLIC_BUCKETS_DEFAULT = "njogis-imagery"
PUBLIC_BUCKETS: set[str] = set(
    filter(None, os.environ.get("S3_PUBLIC_BUCKETS", _PUBLIC_BUCKETS_DEFAULT).split(","))
)

_CREDENTIALS = None
_NO_CREDENTIALS_UNTIL = 0
_CREDENTIALS_LOCK = threading.Lock()


def _fetch_json(url, headers=None):
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=METADATA_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _metadata_token():
    request = Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
    )
    with urlopen(request, timeout=METADATA_TIMEOUT) as response:
        return response.read().decode("utf-8")


def _load_credentials():
    if os.path.exists("/tmp/aws_credentials.json"):
        try:
            if os.path.getsize("/tmp/aws_credentials.json") == 0:
                raise RuntimeError("/tmp/aws_credentials.json is empty")
            with open("/tmp/aws_credentials.json", encoding="utf-8") as f:
                data = json.load(f)
            expires_at = 0
            expiration = data.get("Expiration")
            if expiration:
                expires_at = dt.datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
            
            # The proxy auto-reloads 300 seconds before expires_at
            return {
                "access_key": data["AccessKeyId"],
                "secret_key": data["SecretAccessKey"],
                "token": data.get("SessionToken"),
                "expires_at": expires_at,
            }
        except Exception as exc:
            print(f"s3-sigv4-proxy: failed to load /tmp/aws_credentials.json: {exc}", file=sys.stderr, flush=True)

    access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    token = os.environ.get("AWS_SESSION_TOKEN")
    if access_key and secret_key:
        return {
            "access_key": access_key,
            "secret_key": secret_key,
            "token": token,
            "expires_at": 0,
        }

    full_uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
    relative_uri = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    if full_uri or relative_uri:
        url = full_uri or f"http://169.254.170.2{relative_uri}"
        headers = {}
        auth_token = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        auth_token_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
        if auth_token_file:
            with open(auth_token_file, encoding="utf-8") as token_file:
                auth_token = token_file.read().strip()
        if auth_token:
            headers["Authorization"] = auth_token
        data = _fetch_json(url, headers)
        return _credentials_from_metadata(data)

    if not ENABLE_EC2_METADATA:
        raise RuntimeError("no env or container credentials; EC2 metadata fallback disabled")

    token_headers = {}
    try:
        token_headers["X-aws-ec2-metadata-token"] = _metadata_token()
    except Exception:
        pass
    with urlopen(
        Request("http://169.254.169.254/latest/meta-data/iam/security-credentials/", headers=token_headers),
        timeout=METADATA_TIMEOUT,
    ) as response:
        role_name = response.read().decode("utf-8").splitlines()[0]
    data = _fetch_json(
        f"http://169.254.169.254/latest/meta-data/iam/security-credentials/{role_name}",
        token_headers,
    )
    return _credentials_from_metadata(data)


def _credentials_from_metadata(data):
    expires_at = 0
    expiration = data.get("Expiration")
    if expiration:
        expires_at = dt.datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
    return {
        "access_key": data["AccessKeyId"],
        "secret_key": data["SecretAccessKey"],
        "token": data.get("Token"),
        "expires_at": expires_at,
    }


def credentials():
    global _CREDENTIALS, _NO_CREDENTIALS_UNTIL
    if SIGNING_MODE == "off":
        return None
    with _CREDENTIALS_LOCK:
        if SIGNING_MODE == "auto" and time.time() < _NO_CREDENTIALS_UNTIL:
            return None
        if _CREDENTIALS and (_CREDENTIALS["expires_at"] == 0 or _CREDENTIALS["expires_at"] - time.time() > 300):
            return _CREDENTIALS
        try:
            _CREDENTIALS = _load_credentials()
            return _CREDENTIALS
        except Exception as exc:
            if SIGNING_MODE == "required":
                raise
            _NO_CREDENTIALS_UNTIL = time.time() + 60
            print(f"s3-sigv4-proxy: signing disabled, no credentials found: {exc}", file=sys.stderr, flush=True)
            return None


def _signing_key(secret_key, date_stamp, region):
    date_key = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, SERVICE.encode(), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _canonical_query(query):
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        key, _, value = part.partition("=")
        pairs.append((quote(unquote(key), safe="-_.~"), quote(unquote(value), safe="-_.~")))
    return "&".join(f"{key}={value}" for key, value in sorted(pairs))


def _canonical_uri(path):
    return "/" + "/".join(quote(segment, safe="-_.~") for segment in path.lstrip("/").split("/"))


def signed_headers(method, path, query, range_header, extra_headers=None, bucket=None, region=None, signing_mode=None):
    bucket = bucket or S3_BUCKET
    region = region or S3_REGION
    effective_mode = signing_mode if signing_mode is not None else SIGNING_MODE
    creds = None if effective_mode == "off" else credentials()
    host = f"{bucket}.s3.{region}.amazonaws.com"
    headers = {
        "host": host,
        "x-amz-content-sha256": UNSIGNED_PAYLOAD,
    }
    if range_header:
        headers["range"] = range_header
    if extra_headers:
        headers.update(extra_headers)

    if not creds:
        return {header.title(): value for header, value in headers.items()}

    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    credential_scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"

    headers["x-amz-date"] = amz_date
    if creds.get("token"):
        headers["x-amz-security-token"] = creds["token"]

    signed_header_names = sorted(headers)
    canonical_headers = "".join(f"{name}:{headers[name].strip()}\n" for name in signed_header_names)
    signed_header_list = ";".join(signed_header_names)
    canonical_request = "\n".join(
        [
            method,
            _canonical_uri(path),
            _canonical_query(query),
            canonical_headers,
            signed_header_list,
            UNSIGNED_PAYLOAD,
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(creds["secret_key"], date_stamp, region),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={creds['access_key']}/{credential_scope}, "
        f"SignedHeaders={signed_header_list}, "
        f"Signature={signature}"
    )
    return {header.title(): value for header, value in headers.items()}


class S3ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self._proxy()

    def do_GET(self):
        self._proxy()

    def _proxy(self):
        parsed = urlsplit(self.path)
        
        parts = parsed.path.lstrip("/").split("/", 3)
        if len(parts) >= 4 and parts[0] in ("requester-pays", "standard"):
            req_pays_flag, target_region, target_bucket, upstream_path = parts
            upstream_path = "/" + upstream_path
            is_requester_pays = req_pays_flag == "requester-pays"
        else:
            target_region = S3_REGION
            target_bucket = S3_BUCKET
            upstream_path = parsed.path
            is_requester_pays = False

        range_header = self.headers.get("Range")
        extra = {"x-amz-request-payer": "requester"} if is_requester_pays else None

        # Public buckets in other accounts reject signed cross-account requests
        # with 403 even though unsigned access works fine.  Force unsigned mode
        # for any bucket in PUBLIC_BUCKETS so we never attach an Authorization
        # header to those requests.
        effective_signing = SIGNING_MODE
        if target_bucket in PUBLIC_BUCKETS:
            effective_signing = "off"

        headers = signed_headers(
            self.command,
            upstream_path,
            parsed.query,
            range_header,
            extra,
            bucket=target_bucket,
            region=target_region,
            signing_mode=effective_signing,
        )

        connection = http.client.HTTPSConnection(
            f"{target_bucket}.s3.{target_region}.amazonaws.com",
            timeout=30,
        )
        upstream_url = upstream_path + (f"?{parsed.query}" if parsed.query else "")

        try:
            connection.request(self.command, upstream_url, headers=headers)
            response = connection.getresponse()
            self.send_response_only(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() in {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}:
                    continue
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as exc:
            body = f"S3 signing proxy error: {exc}\n".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        finally:
            connection.close()

    def log_message(self, fmt, *args):
        print(f"s3-sigv4-proxy: {self.address_string()} {fmt % args}", file=sys.stderr, flush=True)


def main():
    print(
        f"s3-sigv4-proxy: listening on {LISTEN_HOST}:{LISTEN_PORT}, "
        f"bucket={S3_BUCKET}, region={S3_REGION}, signing={SIGNING_MODE}, "
        f"public_buckets(unsigned)={sorted(PUBLIC_BUCKETS) or 'none'}",
        flush=True,
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), S3ProxyHandler).serve_forever()


if __name__ == "__main__":
    main()
