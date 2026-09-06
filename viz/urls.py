from django.urls import path

from . import views

app_name = "viz"

urlpatterns = [
    path("",                 views.chart,           name="chart"),
    path("slides",           views.slides,          name="slides"),
    # Static support for the deck served at /slides: it references its assets
    # with paths relative to the repo root, so at "/slides" the browser asks
    # for "/presentation/…", "/docs/img/…" and "/viz/static/viz/…" —
    # `deck_asset` streams those from disk (guarded, see DECK_ROOTS).
    path("presentation/<path:rest>",     views.deck_asset, {"root": "presentation"}, name="deck_presentation"),
    path("docs/<path:rest>",             views.deck_asset, {"root": "docs"},         name="deck_docs"),
    path("viz/static/viz/<path:rest>",   views.deck_asset, {"root": "viz"},          name="deck_viz"),
    path("plot.json",        views.plot_json,       name="plot_json"),
    path("drill.json",       views.drill_json,      name="drill_json"),
    path("txn.json",         views.txn_json,        name="txn_json"),
    path("api/sources",      views.api_sources,     name="api_sources"),
    path("api/source-meta",  views.api_source_meta, name="api_source_meta"),
    # Query console. Grouped under a prefix rather than four more *.json names;
    # all four are login-gated by _login_required_json, unlike everything above.
    path("query/run",            views.query_run,            name="query_run"),
    path("query/cancel",         views.query_cancel,         name="query_cancel"),
    path("query/history",        views.query_history,        name="query_history"),
    path("query/history/delete", views.query_history_delete, name="query_history_delete"),
    # Bridge from the active chart into the SQL console: returns the SQL
    # string the active chart would plot, without running it. Login-gated.
    path("chart/sql",            views.chart_sql,            name="chart_sql"),
]
