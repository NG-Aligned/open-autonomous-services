# Open Autonomous Services (OAS)

A small, dependency-free framework for building **independently configured, cooperating autonomous services**.

The unit of development is the *capability*, not the application. Each service declares what it does in a
manifest, keeps its own persistent state, and cooperates with other services only through **claims** — typed
records that carry their origin, inputs, confidence and history. An application is an arrangement of services.

- **No central dependency.** Python standard library + SQLite. A node is one file. Nodes exchange verifiable
  claim bundles as plain files; nothing requires a server, a broker, or a particular host.
- **Standard contract.** Purpose, configuration, state, triggers, execution, output and continuity are declared
  in one manifest format (JSON or TOML).
- **Provenance by construction.** Claims are content-addressed (SHA-256). Every claim records its source
  service, the claims it was derived from, its confidence and what it supersedes.
- **Enforced permissions.** Services can only read, write, cite and message what their manifest declares.
  Denials are audited.
- **Signed, schema-checked claims.** Claims are Ed25519-signed; importers can require signatures and restrict which
  keys may vouch for which services. Services may declare JSON-Schema-subset contracts for claim content.
- **Safe execution.** Each run commits atomically (claims + state + messages + cursors), with retries,
  exponential backoff, timeouts, a circuit breaker and execution leases. Optional process isolation makes
  timeouts kill the service and contains crashes and memory use.
- **Schedules.** Interval, cron (UTC), event, dependency and message triggers.
- **Decentralised exchange.** Share claims through a synced directory (git, Syncthing, USB) or read-only HTTP.
- **Orchestration without central intelligence.** Execution order is derived from the manifests' declared
  reads/writes; the orchestrator only coordinates.

> Status: **v0.2.0**, built within a 60-minute work budget. See [docs/STATUS.md](docs/STATUS.md) for exactly what
> was verified, what was not, and the known limitations. Please read it before relying on this.

## Quick start

Requires Python 3.11+ (tested on 3.11, 3.12 and 3.13, Linux).

```bash
pip install .            # or run in-place with: python -m oas ...
oas --db demo.db discover examples/research_pipeline
oas --db demo.db tick                       # runs monitor -> evaluator -> reporter in dependency order
oas --db demo.db claims --type report
oas --db demo.db lineage <claim-id>         # full provenance ancestry of a claim
oas --db demo.db graph                      # dependency graph, execution order, cycles
oas --db demo.db export bundle.json         # share verified claims with another node
oas --db demo.db whoami                     # this node's id and public signing key
oas --db other.db trust add <pubkey> --label demo --service 'literature_*'
oas --db other.db import bundle.json --require-trusted    # only accept claims signed by trusted keys
oas --db demo.db sync ./shared              # or: exchange through a shared directory
oas --db demo.db prune --keep-runs 50       # retention
oas --db demo.db daemon --interval 5        # run the scheduler until stopped (SIGINT/SIGTERM)
```

Writing a service:

```python
from oas import Orchestrator, Store

store = Store("demo2.db")
orch = Orchestrator(store)

def greeter(ctx):
    ctx.write("greeting", {"text": f"hello {ctx.config['name']}"}, confidence=1.0)

orch.register({
    "id": "greeter", "version": "1.0.0", "purpose": "say hello",
    "config": {"name": "world"},
    "writes": ["greeting"],
    "triggers": [{"kind": "interval", "seconds": 60}],
}, greeter)

orch.tick()
print(store.query_claims(["greeting"])[0].content)   # {'text': 'hello world'}
```

Or declare the service in a JSON/TOML manifest with `"entrypoint": "module:function"` and use
`oas discover <directory>` (see `examples/research_pipeline/`).

## Tests

```bash
python -m unittest discover -s tests
python benchmarks/bench.py 20000          # measure on your machine
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the design, semantics, and a requirement → code → test map.
- [docs/STATUS.md](docs/STATUS.md) — verification record, what is unfinished, and honest limitations.

## License

Apache-2.0 (see `LICENSE`).
