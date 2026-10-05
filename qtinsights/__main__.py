"""Usage:
  python3 -m qtinsights serve [port]      open the dashboard (default port 8765)
  python3 -m qtinsights demo [port]       open the dashboard on made-up data (default port 8766)
  python3 -m qtinsights demo chase [port] "chase the winner" vs the S&P 500, real prices (port 8766)
  python3 -m qtinsights sync [token]      pull new data from Questrade
  python3 -m qtinsights import FILE       load a questrade_export.json
  python3 -m qtinsights check             print data problems found
  python3 -m qtinsights debug-prices SYM  show Questrade's raw price response for one symbol
"""
import sys

from . import analytics, config
from .questrade import AuthError
from .store import Store


def main(argv):
    cmd = argv[0] if argv else "serve"
    if cmd == "demo":
        from .server import serve
        variant = argv[1] if len(argv) > 1 and not argv[1].isdigit() else "simple"
        rest = [a for a in argv[1:] if a.isdigit()]
        config.enable_demo(variant)
        config.DB_FILE.unlink(missing_ok=True)  # rebuilt fresh, so it always ends today
        if variant == "chase":
            from . import demo_chase
            print("Fetching prices from Yahoo Finance (cached for the day)…", file=sys.stderr)
            demo_chase.build(Store(config.DB_FILE))
        else:
            from . import demo
            demo.build(Store(config.DB_FILE))
        serve(int(rest[0]) if rest else 8766)
        return
    if cmd == "serve":
        from .server import serve
        serve(int(argv[1]) if len(argv) > 1 else 8765)
        return
    store = Store(config.DB_FILE)
    if cmd == "sync":
        from .sync import sync
        try:
            sync(store, argv[1] if len(argv) > 1 else None,
                 log=lambda m, end="\n": print(m, end=end, file=sys.stderr, flush=True))
        except AuthError as e:
            sys.exit(str(e))
    elif cmd == "import" and len(argv) > 1:
        from .sync import import_export
        import_export(store, argv[1])
    elif cmd == "debug-prices" and len(argv) > 1:
        debug_prices(store, argv[1])
    elif cmd == "check":
        issues = analytics.build(store)["issues"]
        print("\n".join(issues) if issues else "No problems found.")
    else:
        sys.exit(__doc__)


def debug_prices(store, sym):
    """Ask Questrade for the last 10 days of one symbol and print exactly what comes back."""
    import json
    from datetime import datetime, timedelta, timezone
    from .questrade import Client
    sid = store.symbols().get(sym, (None,))[0]
    if not sid:
        sys.exit(f"{sym} has no Questrade symbol ID yet; run a sync first.")
    try:
        client = Client(config.TOKEN_FILE)
    except AuthError as e:
        sys.exit(str(e))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    for label, s, e in [("documented format", start, end)]:
        try:
            got = client.candles(sid, s, e)
            print(f"{sym} ({sid}), {label}: {json.dumps(got)[:1500]}")
        except Exception as ex:
            body = getattr(getattr(ex, "response", None), "text", "")
            print(f"{sym} ({sid}), {label}: ERROR {ex} {body[:500]}")


if __name__ == "__main__":
    main(sys.argv[1:])
