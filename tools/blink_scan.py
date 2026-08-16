"""Scan the HA recorder database for cooling-switch "blinks".

A blink is a zone switch that turned on and back off in under 150 s —
the fingerprint of the slot double-booking fixed in v1.5.0 (and of the
restart over-admission, also fixed there). Run it against a live system
to verify the fix: blinks should be ~zero, excepting genuine short
requests (check the matching request switch).

Runs inside the Home Assistant container, read-only against the
recorder. From a machine with SSH access to the Docker host:

    ssh root@<host> 'docker exec -i HomeAssistant python3 -' < blink_scan.py

Adjust the LIKE pattern if your switches are not named switch.cooling*.
"""
import sqlite3, datetime

con = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True, timeout=30)
cur = con.cursor()
cur.execute(
    "SELECT metadata_id, entity_id FROM states_meta "
    "WHERE entity_id LIKE 'switch.cooling%'"
)
metas = cur.fetchall()
print("entities:", [e for _, e in metas])

for mid, eid in metas:
    cur.execute(
        "SELECT state, last_updated_ts FROM states "
        "WHERE metadata_id=? ORDER BY last_updated_ts",
        (mid,),
    )
    rows = cur.fetchall()
    events = []
    prev = None
    for st, ts in rows:
        if ts is None:
            continue
        if st != prev:
            events.append((st, ts))
            prev = st
    ons = 0
    shorts = []
    for i, (st, ts) in enumerate(events):
        if st == "on" and i + 1 < len(events):
            nst, nts = events[i + 1]
            dur = nts - ts
            ons += 1
            if dur < 150:
                shorts.append((ts, dur, nst))
    first = datetime.datetime.fromtimestamp(events[0][1]).isoformat() if events else "n/a"
    print(f"\n{eid}: {ons} on-cycles since {first}, {len(shorts)} shorter than 150s")
    for ts, dur, nst in shorts:
        t = datetime.datetime.fromtimestamp(ts).isoformat()
        print(f"  {t}  on for {dur:6.1f}s -> {nst}")
