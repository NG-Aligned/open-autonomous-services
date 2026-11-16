#!/usr/bin/env bash
# Two independent nodes. B accepts A's claims only if A's key is trusted for those services,
# and rejects any claim altered in transit, even one that still comes from a trusted key.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OAS="${OAS:-python3 -m oas}"
export PYTHONPATH="${PYTHONPATH:-$HERE/..}"
WORK="$(mktemp -d)"; cd "$WORK"
j() { python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }

$OAS --db a.db discover "$HERE/research_pipeline" >/dev/null
$OAS --db a.db tick >/dev/null
PUB=$($OAS --db a.db whoami | j "print(d['public_key'])")
$OAS --db a.db export bundle.json >/dev/null
$OAS --db b.db init >/dev/null
echo "node A signed claims with key ${PUB:0:16}..."

echo "1. B imports with no trusted signers:"
$OAS --db b.db import bundle.json --require-trusted | j "print('   imported', d['imported'], '| rejected', len(d['rejected']), '|', d['rejected'][0]['reason'])"

$OAS --db b.db trust add "$PUB" --label node-a --service 'literature_*' --service 'evidence_*' >/dev/null
echo "2. B trusts A for literature_* and evidence_* only (not reporting_service):"
$OAS --db b.db import bundle.json --require-trusted | j "print('   imported', d['imported'], '| rejected', len(d['rejected']), '|', d['rejected'][0]['reason'])"

python3 - <<'PY'
import json
b = json.load(open("bundle.json"))
for c in b["claims"]:
    if c["type"] == "evidence.assessment":
        c["origin"] = "forged-node"          # id is unchanged; only the signature can catch this
json.dump(b, open("tampered.json", "w"))
PY
$OAS --db c.db init >/dev/null
$OAS --db c.db trust add "$PUB" --label node-a --service '*' >/dev/null
echo "3. C trusts A for everything, but receives a bundle with one field altered in transit:"
$OAS --db c.db import tampered.json --require-trusted | j "print('   imported', d['imported'], '| rejected', len(d['rejected']), '|', d['rejected'][0]['reason'])"
