from django.urls import path
from . import views

app_name = "sources"

urlpatterns = [
    # Pages
    path("",                                           views.source_list,    name="list"),
    path("settings/",                                  views.settings_page,  name="settings"),
    path("new/",                                       views.source_new,     name="new"),
    path("<int:pk>/",                                  views.source_detail,  name="detail"),
    path("<int:pk>/edit/",                             views.source_edit,    name="edit"),
    path("<int:pk>/delete/",                           views.source_delete,  name="delete"),
    # Async JSON API
    path("<int:pk>/test-connection/",                  views.test_connection,  name="test_connection"),
    path("<int:pk>/connection-status/",                 views.connection_status, name="connection_status"),
    path("<int:pk>/preview-import/",                   views.preview_import,   name="preview_import"),
    path("<int:pk>/etl/run/",                          views.etl_run,          name="etl_run"),
    path("<int:pk>/etl/status/",                       views.etl_status,       name="etl_status"),
    path("<int:pk>/etl/logs/",                          views.etl_logs,         name="etl_logs"),
    path("<int:pk>/etl/log/",                           views.etl_log_tail,     name="etl_log_tail"),
    path("<int:pk>/etl/kill/",                          views.etl_kill,         name="etl_kill"),
    path("<int:pk>/etl/forever/start/",                 views.etl_forever_start, name="etl_forever_start"),
    path("<int:pk>/etl/forever/stop/",                  views.etl_forever_stop,  name="etl_forever_stop"),
    path("<int:pk>/etl/forever/status/",                views.etl_forever_status,name="etl_forever_status"),
    path("<int:pk>/etl/delete-from/",                  views.etl_delete_from,       name="etl_delete_from"),
    path("<int:pk>/etl/truncate-last/",                views.etl_truncate_last,    name="etl_truncate_last"),
    path("<int:pk>/etl/truncate-partition/",            views.etl_truncate_partition, name="etl_truncate_partition"),
    path("<int:pk>/etl/drop-partition/",                views.etl_drop_partition,     name="etl_drop_partition"),
    path("<int:pk>/etl/vacuum-analyze/",               views.etl_vacuum_analyze,   name="etl_vacuum_analyze"),
    path("<int:pk>/etl/set-highwatermark/",            views.etl_set_highwatermark, name="etl_set_highwatermark"),
    path("<int:pk>/etl/clear-history/",                 views.etl_clear_history,    name="etl_clear_history"),
    path("<int:pk>/etl/create-partitions/",             views.etl_create_partitions, name="etl_create_partitions"),
    path("<int:pk>/etl/refresh-counts/",                views.etl_refresh_counts,    name="etl_refresh_counts"),
    path("<int:pk>/etl/audit/",                         views.etl_audit,             name="etl_audit"),
    path("<int:pk>/etl/audit-ids/",                     views.etl_audit_ids,         name="etl_audit_ids"),
    path("<int:pk>/etl/audit-id-details/",              views.etl_audit_id_details,  name="etl_audit_id_details"),
    path("<int:pk>/etl/backfill/",                      views.etl_backfill,          name="etl_backfill"),
    path("<int:pk>/cache/clear/",                       views.cache_clear,           name="cache_clear"),
    # Fact columns CRUD (JSON)
    path("<int:pk>/fact-columns/<int:fc_id>/estimate-cardinality/", views.estimate_cardinality, name="fc_estimate_cardinality"),
    path("<int:pk>/fact-columns/add/",                  views.fc_add,    name="fc_add"),
    path("<int:pk>/fact-columns/<int:fc_id>/edit/",     views.fc_edit,   name="fc_edit"),
    path("<int:pk>/fact-columns/<int:fc_id>/delete/",   views.fc_delete, name="fc_delete"),
    # Dimensions CRUD (JSON)
    path("<int:pk>/dimensions/add/",                    views.dim_add,    name="dim_add"),
    path("<int:pk>/dimensions/<int:dim_id>/edit/",      views.dim_edit,   name="dim_edit"),
    path("<int:pk>/dimensions/<int:dim_id>/delete/",    views.dim_delete, name="dim_delete"),
    path("<int:pk>/dimensions/<int:dim_id>/values/",    views.dim_values,        name="dim_values"),
    # Group dimensions (named group sets, arbitrarily many per dimension)
    path("<int:pk>/group-dims/",                        views.group_dims_list,   name="group_dims_list"),
    path("<int:pk>/group-dims/add/",                    views.group_dim_add,     name="group_dim_add"),
    path("<int:pk>/group-dims/<int:gs_id>/",            views.group_dim_detail,  name="group_dim_detail"),
    path("<int:pk>/group-dims/<int:gs_id>/save/",       views.group_dim_save,    name="group_dim_save"),
    path("<int:pk>/group-dims/<int:gs_id>/delete/",     views.group_dim_delete,  name="group_dim_delete"),
    # Indices CRUD (JSON)
    path("<int:pk>/indices/add/",                       views.idx_add,    name="idx_add"),
    path("<int:pk>/indices/<int:idx_id>/edit/",         views.idx_edit,   name="idx_edit"),
    path("<int:pk>/indices/<int:idx_id>/delete/",       views.idx_delete, name="idx_delete"),
    # Measures CRUD (JSON)
    path("<int:pk>/measures/add/",                      views.measure_add,    name="measure_add"),
    path("<int:pk>/measures/<int:measure_id>/edit/",    views.measure_edit,   name="measure_edit"),
    path("<int:pk>/measures/<int:measure_id>/delete/",  views.measure_delete, name="measure_delete"),
    # Schema actions (JSON)
    path("<int:pk>/create-fact/",                      views.create_fact_table,    name="create_fact_table"),
    path("<int:pk>/sync-schema/",                      views.sync_schema_view,     name="sync_schema"),
    path("<int:pk>/import-dims/",                       views.import_dims,         name="import_dims"),
    path("<int:pk>/dim-data/",                          views.dim_data,            name="dim_data"),
    path("<int:pk>/dim-data/<str:dim_table>/",          views.dim_data_single,     name="dim_data_single"),
    path("<int:pk>/indices/create-all/",                views.create_all_indices,  name="create_all_indices"),
    path("<int:pk>/indices/drop-all/",                  views.drop_all_indices,    name="drop_all_indices"),
    # Source profiles (JSON) — catalogue is datasource-independent, so it
    # sits above <int:pk>/. "profiles" cannot collide with the int converter.
    path("profiles/",                                   views.profiles_list,       name="profiles_list"),
    path("profiles/upload/",                            views.profile_upload,      name="profile_upload"),
    path("profiles/<str:slug>/",                        views.profile_detail,      name="profile_detail"),
    path("profiles/<str:slug>/delete/",                 views.profile_delete,      name="profile_delete"),
    path("<int:pk>/profile/save/",                      views.profile_save,        name="profile_save"),
    path("<int:pk>/profile/download/",                  views.profile_download,    name="profile_download"),
    path("<int:pk>/profile/apply/",                     views.profile_apply,       name="profile_apply"),
]
