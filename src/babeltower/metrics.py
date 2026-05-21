from prometheus_client import Counter, Gauge

intents_created_total = Counter(
    "babeltower_intents_created_total",
    "Total intents created.",
)
searches_total = Counter(
    "babeltower_searches_total",
    "Total signed search requests.",
)
active_sessions = Gauge(
    "babeltower_active_sessions",
    "Currently active websocket sessions.",
)
messages_relayed_total = Counter(
    "babeltower_messages_relayed_total",
    "Total websocket messages accepted for relay.",
)
matches_confirmed_total = Counter(
    "babeltower_matches_confirmed_total",
    "Total matches confirmed.",
)
soft_bans_applied_total = Counter(
    "babeltower_soft_bans_applied_total",
    "Total soft bans applied.",
)
