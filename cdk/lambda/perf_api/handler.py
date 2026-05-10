"""
Returns the most recent N WMS GetMap requests + upstream_time_s, parsed
from the json_metrics nginx access log lines in CloudWatch Logs.
Client-side computes percentiles on the array.
"""
import json
import os
import time

import boto3

LOG_GROUP = os.environ.get("LOG_GROUP", "/ecs/mapserver")
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "1"))

logs = boto3.client("logs")

QUERY = """
fields @timestamp, args, upstream_time_s, status, size
| filter args like /GetMap/
| sort @timestamp desc
| limit 100
"""

# Function URL's CORS config injects Access-Control-* headers automatically.
# Setting them here too produces duplicate headers and browsers reject the
# response with "Failed to fetch." Only set non-CORS headers in the Lambda.
HEADERS = {"Cache-Control": "no-store"}


def _query():
    end = int(time.time())
    start = end - WINDOW_HOURS * 3600
    qid = logs.start_query(
        logGroupName=LOG_GROUP,
        startTime=start,
        endTime=end,
        queryString=QUERY,
        limit=100,
    )["queryId"]

    deadline = time.time() + 25
    while time.time() < deadline:
        r = logs.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            return r
        time.sleep(0.4)
    raise TimeoutError("Logs Insights query did not complete in time")


def handler(event, context):
    try:
        result = _query()
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {**HEADERS, "Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }

    requests_out = []
    for row in result.get("results", []):
        rec = {f["field"]: f["value"] for f in row}
        try:
            t = float(rec.get("upstream_time_s") or 0)
        except ValueError:
            t = 0.0
        requests_out.append({
            "timestamp": rec.get("@timestamp"),
            "upstream_time_s": t,
            "size": int(rec.get("size") or 0),
            "status": int(rec.get("status") or 0),
            "args": rec.get("args"),
        })

    body = {
        "requests": requests_out,
        "count": len(requests_out),
        "window_hours": WINDOW_HOURS,
        "scanned_records": result.get("statistics", {}).get("recordsScanned"),
    }
    return {
        "statusCode": 200,
        "headers": {**HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(body),
    }
