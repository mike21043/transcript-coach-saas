#!/usr/bin/env bash
set -euo pipefail
if [ -z "${GITHUB_PAT:-}" ]; then echo "ERROR: GITHUB_PAT not set"; exit 2; fi
OWNER="mike21043"
REPO="transcript-coach-saas"
WORKFLOW="smoke-published.yml"
REF="local-save-20250920-225817"
API_BASE="https://api.github.com/repos/$OWNER/$REPO"
DISPATCH_URL="$API_BASE/actions/workflows/$WORKFLOW/dispatches"

echo "Dispatching workflow $WORKFLOW on ref $REF"
http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_PAT" "$DISPATCH_URL" -d "{\"ref\":\"$REF\"}")
if [ "$http_code" -ne 204 ]; then
  echo "Dispatch failed (http $http_code). Response body:";
  curl -s -X POST -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_PAT" "$DISPATCH_URL" -d "{\"ref\":\"$REF\"}" || true
  exit 3
fi

echo "Dispatch accepted (204). Waiting for run to be created..."
runid=""
for i in $(seq 1 45); do
  sleep 2
  runs_json=$(curl -s -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_PAT" "$API_BASE/actions/workflows/$WORKFLOW/runs?per_page=20" ) || true
  runid=$(echo "$runs_json" | jq -r '.workflow_runs[] | select(.head_branch=="'"$REF"'" ) | .id' | head -n1 || true)
  if [ -n "$runid" ]; then
    echo "Found run id: $runid"
    break
  fi
done
if [ -z "$runid" ]; then echo "Timed out waiting for a run to appear."; exit 4; fi

echo "Polling run $runid status..."
status=""
conclusion=""
for i in $(seq 1 300); do
  sleep 4
  run_json=$(curl -s -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_PAT" "$API_BASE/actions/runs/$runid") || true
  status=$(echo "$run_json" | jq -r .status)
  conclusion=$(echo "$run_json" | jq -r .conclusion)
  echo "[$(date +%T)] status=$status conclusion=$conclusion"
  if [ "$status" = "completed" ]; then
    echo "Run completed with conclusion=$conclusion"
    break
  fi
done

if [ "$status" != "completed" ]; then echo "Run did not complete in time."; exit 5; fi

echo "Jobs summary for run $runid:"
curl -s -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $GITHUB_PAT" "$API_BASE/actions/runs/$runid/jobs" | jq -r '.jobs[] | "- id:" + (.id|tostring) + " name:" + (.name//"") + " status:" + (.status//"") + " conclusion:" + (.conclusion//"")'

if [ "$conclusion" != "success" ]; then
  echo "Run conclusion is $conclusion. Logs URL: $API_BASE/actions/runs/$runid/logs"
fi

echo "Done." 
