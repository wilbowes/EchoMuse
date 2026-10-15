# Controller modules: where em_api's code went

`controller/em_api.py` was 6,680 lines. Between 2026-10-15 and 10-16 it was
split into the modules below (#907–#940), one area per pull request, each a
pure move: bodies and comments unchanged apart from names. em_api now holds
only the app assembly (`create_app`), its two middlewares, session pruning and
the hooks em_controller calls.

**If you have a fork or an open pull request against em_api**, find each name
you touched in the table, and rebase with moved code shown as moved:

```bash
git diff --color-moved=zebra --color-moved-ws=allow-indentation-change main...your-branch
```

A name that kept its leading underscore is still private to its new module.
One that lost it is now a module's interface, and callers say
`module.name(...)`; em_api re-exports nothing.

`em_controller` and `em_esphome` have not been split yet. Only
`handle_control` changed shape there: one `_on_<type>` handler per control
message, looked up in `CONTROL_HANDLERS` (#910).

## The modules

| Module | What it holds |
|---|---|
| `em_http` | The API's response shape and request helpers. |
| `em_events` | Live events pushed to every open dashboard tab. |
| `em_shell` | The device shell transport: sessions, locks, file transfer. |
| `em_releases` | Firmware, controller and emOS release feeds, and the release cache. |
| `em_payloads` | Files kept in step on every device (start script, debloat pair, controller address list). |
| `em_ota` | Firmware updates and the fleet-wide transfer queue. |
| `em_oww_sync` | Installing on-device wake word assets. |
| `em_emos_ota` | The emOS update and its release assets. |
| `em_reconcile` | What a device has installed, brought in line when it connects. |
| `em_process_stats` | The controller's own health: uptime, CPU, memory, the log ring. |
| `em_state` | Process-wide state: the device registry, shell sessions, model caches, link ports. |
| `em_device_view` | How a device is presented: its /api/devices record, WiFi-change and link-refusal state. |
| `em_device_config` | Saving config: the live push, the drop guard, the console password and timeout. |
| `em_credentials` | Issuing device-link credentials (token and CA) to an approved Echo over its shell plane. |
| `em_ingressauth` | Home Assistant ingress: the deployment flag and the trust decision. |
| `em_config_types` | Config value types. |
| `em_routes_activity` | Routes: the Activity tab (turns, audio, wake samples). |
| `em_routes_auth` | Routes: sign-in, sessions, users. |
| `em_routes_config` | Routes: fleet config. |
| `em_routes_devices` | Routes: the device list and one device (rename, approve, delete, config, WiFi, logs). |
| `em_routes_events` | Routes: the live events WebSocket. |
| `em_routes_link` | Routes: device-link credentials and pairing. |
| `em_routes_oww` | Routes: custom wake word models and a device's wake word assets. |
| `em_routes_provision` | Routes: the provisioning wizard. |
| `em_routes_shell` | Routes: the dashboard terminal. |
| `em_routes_static` | Routes: the pages and first-run setup. |
| `em_routes_system` | Routes: status, controller settings, support bundle. |
| `em_routes_updates` | Routes: the Updates tab (firmware, emOS, releases, maintenance). |

## Old em_api names

Grouped by where each one lives now.

### `em_http`

| Was in em_api | Now |
|---|---|
| `_error` | `em_http.error` |
| `_json_body` | `em_http.json_body` |
| `_ok` | `em_http.ok` |
| `_require_str` | `em_http.require_str` |

### `em_events`

| Was in em_api | Now |
|---|---|
| `_event_clients` | `em_events.clients` |
| `_push_event` | `em_events.push` |
| `_push_log_event` | `em_events.push_log` |

### `em_shell`

| Was in em_api | Now |
|---|---|
| `CRASH_LOG_RETRY_S` | `em_shell.CONNECT_RETRY_S` |
| `_exec_shell` | `em_shell.send_command` |
| `_get_device_shell_ws` | `em_shell.get_ws` |
| `_release_shell_lock` | `em_shell.release_lock` |
| `_release_shell_ws` | `em_shell.release_ws` |
| `_shell_lock` | `em_shell.locks` |
| `_SHELL_OK` | `em_shell.OK_MARK` |
| `_shell_owner` | `em_shell.owners` |
| `_shell_run` | `em_shell.run` |
| `_shell_ws` | `em_shell.sockets` |
| `_stream_file_to_device` | `em_shell.stream_file` |
| `_transfer_failed` | `em_shell._transfer_failed` |
| `_TRANSFER_STAGES` | `em_shell._TRANSFER_STAGES` |
| `TransferResult` | `em_shell.TransferResult` |

### `em_releases`

| Was in em_api | Now |
|---|---|
| `_controller_cache` | `em_releases._controller_cache` |
| `_controller_cache_ts` | `em_releases._controller_cache_ts` |
| `_emos_latest` | `em_releases._emos_latest` |
| `_emos_latest_version` | `em_releases.emos_latest_version` |
| `_fetch_binary` | `em_releases.download` |
| `_fetch_controller_release` | `em_releases.fetch_controller` |
| `_fetch_latest_emos_release` | `em_releases.fetch_emos` |
| `_fetch_latest_release` | `em_releases.fetch_firmware` |
| `_get_cached_release` | `em_releases.cached_firmware` |
| `GITHUB_API_URL` | `em_releases.GITHUB_API_URL` |
| `GITHUB_TAG_OBJECT_URL` | `em_releases.GITHUB_TAG_OBJECT_URL` |
| `GITHUB_TAGS_URL` | `em_releases.GITHUB_TAGS_URL` |
| `_release_cache` | `em_releases._release_cache` |
| `_release_cache_ts` | `em_releases._release_cache_ts` |
| `RELEASE_CACHE_TTL` | `em_releases.RELEASE_CACHE_TTL` |
| `release_poll_loop` | `em_releases.poll_loop` |
| `_remember_emos_latest` | `em_releases._remember_emos` |
| `_update_check_interval` | `em_releases.check_interval` |

### `em_payloads`

| Was in em_api | Now |
|---|---|
| `_controller_endpoints_file` | `em_payloads.endpoints_file` |
| `_debloat_packages` | `em_payloads.debloat_packages` |
| `DEBLOAT_SCRIPT_PATH` | `em_payloads.DEBLOAT_SCRIPT_PATH` |
| `ENDPOINTS_RETRY_S` | `em_payloads.ENDPOINTS_RETRY_S` |
| `PAYLOADS_DIR` | `em_payloads.PAYLOADS_DIR` |
| `_push_controller_endpoints` | `em_payloads.push_endpoints` |
| `_sync_controller_endpoints` | `em_payloads.sync_endpoints` |
| `_sync_debloat` | `em_payloads.sync_debloat` |
| `_sync_start_script` | `em_payloads.sync_start_script` |

### `em_ota`

| Was in em_api | Now |
|---|---|
| `_extract_binary_version` | `em_ota.extract_binary_version` |
| `_monitor_reconnect` | `em_ota._monitor_reconnect` |
| `_ota_lock` | `em_ota.queue` |
| `OTA_MAX_HOLD_S` | `em_ota.MAX_HOLD_S` |
| `_run_rollback` | `em_ota.run_rollback` |
| `_run_update` | `em_ota.run_update` |
| `_run_update_locked` | `em_ota._run_update_locked` |
| `_stream_binary_to_slot` | `em_ota._stream_binary_to_slot` |
| `_supervisor_log_wanted` | `em_ota.supervisor_log_wanted` |
| `_update_errors` | `em_ota.errors` |
| `_update_failed` | `em_ota._update_failed` |
| `_updates_in_progress` | `em_ota.in_progress` |
| `_updates_queued` | `em_ota.queued` |

### `em_oww_sync`

| Was in em_api | Now |
|---|---|
| `_hold_back_oww_model` | `em_oww_sync.hold_back_model` |
| `_install_then_switch` | `em_oww_sync.install_then_switch` |
| `_oww_device_state` | `em_oww_sync.device_state` |
| `_oww_wanted_models` | `em_oww_sync.wanted_models` |
| `reconcile_oww_assets` | `em_oww_sync.reconcile` |
| `_sync_oww_assets` | `em_oww_sync.sync` |
| `_sync_oww_assets_locked` | `em_oww_sync._sync_locked` |

### `em_emos_ota`

| Was in em_api | Now |
|---|---|
| `EMOS_INIT_ASSETS` | `em_emos_ota.INIT_ASSETS` |
| `_emos_log` | `em_emos_ota._emos_log` |
| `EMOS_OTA_MAX_HOLD_S` | `em_emos_ota.MAX_HOLD_S` |
| `EMOS_PAYLOAD_ASSET` | `em_emos_ota.PAYLOAD_ASSET` |
| `EMOS_SBIN_ASSETS` | `em_emos_ota.SBIN_ASSETS` |
| `EMOS_SBIN_BOTH_ARCHES` | `em_emos_ota.SBIN_BOTH_ARCHES` |
| `_emos_sh` | `em_emos_ota._emos_sh` |
| `_emos_status_on_connect` | `em_emos_ota.status_on_connect` |
| `_emos_update_busy` | `em_emos_ota.busy` |
| `_emos_update_errors` | `em_emos_ota.errors` |
| `_emos_update_failed` | `em_emos_ota._update_failed` |
| `_emos_update_stage` | `em_emos_ota.stage` |
| `_emos_updates_in_progress` | `em_emos_ota.in_progress` |
| `_emos_updates_queued` | `em_emos_ota.queued` |
| `_EmosIO` | `em_emos_ota._EmosIO` |
| `_fetch_emos_init` | `em_emos_ota.fetch_init` |
| `_fetch_emos_payload` | `em_emos_ota.fetch_payload` |
| `_fetch_one_init` | `em_emos_ota._fetch_one_init` |
| `_run_emos_update` | `em_emos_ota.run_update` |
| `_run_emos_update_locked` | `em_emos_ota._run_update_locked` |
| `_select_emos_payload` | `em_emos_ota.select_payload` |

### `em_reconcile`

| Was in em_api | Now |
|---|---|
| `_collect_crash_log` | `em_reconcile._collect_crash_log` |
| `forget_reconcile` | `em_reconcile.forget` |
| `_last_reconcile` | `em_reconcile.stamps` |
| `RECONCILE_DEBOUNCE_S` | `em_reconcile.DEBOUNCE_S` |
| `_reconcile_due` | `em_reconcile._due` |
| `reconcile_on_connect` | `em_reconcile.on_connect` |

### `em_process_stats`

| Was in em_api | Now |
|---|---|
| `_controller_stats` | `em_process_stats.controller_stats` |
| `_cpu_history` | `em_process_stats.cpu_history` |
| `CPU_SAMPLE_INTERVAL_S` | `em_process_stats.CPU_SAMPLE_INTERVAL_S` |
| `install_log_ring` | `em_process_stats.install_log_ring` |
| `_log_ring` | `em_process_stats.log_ring` |
| `_proc_meminfo` | `em_process_stats._proc_meminfo` |
| `_PROCESS_START` | `em_process_stats.PROCESS_START` |
| `_read_first_line` | `em_process_stats._read_first_line` |
| `sample_cpu` | `em_process_stats.sample_cpu` |

### `em_state`

| Was in em_api | Now |
|---|---|
| `_link` | `em_state.link` |
| `set_link_ports` | `em_state.set_link_ports` |
| `set_tls_dir` | `em_state.set_tls_dir` |
| `_tls_dir` | `em_state.tls_dir` |

### `em_device_view`

| Was in em_api | Now |
|---|---|
| `clear_link_refused` | `em_device_view.clear_link_refused` |
| `_health_json` | `em_device_view._health_json` |
| `_link_refusals` | `em_device_view._link_refusals` |
| `_listen_json` | `em_device_view._listen_json` |
| `_merge_device` | `em_device_view.merge_device` |
| `note_link_refused` | `em_device_view.note_link_refused` |
| `_row_sections` | `em_device_view._row_sections` |
| `_stored_volume` | `em_device_view._stored_volume` |
| `_WIFI_PENDING_TTL` | `em_device_view._WIFI_PENDING_TTL` |
| `wifi_record_result` | `em_device_view.wifi_record_result` |
| `wifi_state` | `em_device_view.wifi_state` |
| `_wifi_states` | `em_device_view._wifi_states` |

### `em_device_config`

| Was in em_api | Now |
|---|---|
| `_apply_live_config` | `em_device_config.apply_live_config` |
| `_CONSOLE_PW_KEY` | `em_device_config._CONSOLE_PW_KEY` |
| `_CONSOLE_TMOUT_KEY` | `em_device_config._CONSOLE_TMOUT_KEY` |
| `_CONSOLE_TMOUT_MAX` | `em_device_config._CONSOLE_TMOUT_MAX` |
| `_dropped_keys` | `em_device_config.dropped_keys` |
| `_redact_console_pw` | `em_device_config.redact_console_pw` |
| `_resolve_console_pw` | `em_device_config.resolve_console_pw` |
| `_validate_console_timeout` | `em_device_config.validate_console_timeout` |

### `em_credentials`

| Was in em_api | Now |
|---|---|
| `DEVICE_TLS_DIR` | `em_credentials.DEVICE_TLS_DIR` |
| `_issue_credentials` | `em_credentials.issue_credentials` |

### `em_ingressauth`

| Was in em_api | Now |
|---|---|
| `INGRESS_GATEWAY_IP` | `em_ingressauth.INGRESS_GATEWAY_IP` |
| `INGRESS_ONLY` | `em_ingressauth.INGRESS_ONLY` |

### `em_config_types`

| Was in em_api | Now |
|---|---|
| `_well_typed` | `em_config_types.well_typed` |

### `em_routes_activity`

| Was in em_api | Now |
|---|---|
| `_delete_wake_sample` | `em_routes_activity._delete_wake_sample` |
| `_get_device_activity` | `em_routes_activity._get_device_activity` |
| `_get_device_turns` | `em_routes_activity._get_device_turns` |
| `_get_turn_audio` | `em_routes_activity._get_turn_audio` |
| `_get_wake_sample_audio` | `em_routes_activity._get_wake_sample_audio` |
| `_get_wake_samples` | `em_routes_activity._get_wake_samples` |
| `_patch_wake_sample` | `em_routes_activity._patch_wake_sample` |
| `_redact_turns_for` | `em_routes_activity._redact_turns_for` |
| `_slug` | `em_routes_activity._slug` |

### `em_routes_auth`

| Was in em_api | Now |
|---|---|
| `_get_me` | `em_routes_auth._get_me` |
| `_get_users` | `em_routes_auth._get_users` |
| `_patch_user` | `em_routes_auth._patch_user` |
| `_post_change_password` | `em_routes_auth._post_change_password` |
| `_post_ingress_login` | `em_routes_auth._post_ingress_login` |
| `_post_login` | `em_routes_auth._post_login` |
| `_post_logout` | `em_routes_auth._post_logout` |

### `em_routes_config`

| Was in em_api | Now |
|---|---|
| `_get_global_config` | `em_routes_config._get_global_config` |
| `_post_global_config` | `em_routes_config._post_global_config` |

### `em_routes_devices`

| Was in em_api | Now |
|---|---|
| `_delete_device` | `em_routes_devices._delete_device` |
| `_disconnect_device` | `em_routes_devices._disconnect_device` |
| `_get_ble_proxy_key` | `em_routes_devices._get_ble_proxy_key` |
| `_get_device` | `em_routes_devices._get_device` |
| `_get_device_config` | `em_routes_devices._get_device_config` |
| `_get_device_logs` | `em_routes_devices._get_device_logs` |
| `_get_devices` | `em_routes_devices._get_devices` |
| `_get_pending` | `em_routes_devices._get_pending` |
| `_get_sendspin_token` | `em_routes_devices._get_sendspin_token` |
| `_patch_device` | `em_routes_devices._patch_device` |
| `_post_approve` | `em_routes_devices._post_approve` |
| `_post_device_config` | `em_routes_devices._post_device_config` |
| `_post_device_wifi` | `em_routes_devices._post_device_wifi` |
| `_post_device_wifi_scan` | `em_routes_devices._post_device_wifi_scan` |
| `_require_label` | `em_routes_devices._require_label` |
| `_require_unique_label` | `em_routes_devices._require_unique_label` |
| `SENDSPIN_TOKEN_TIMEOUT_S` | `em_routes_devices.SENDSPIN_TOKEN_TIMEOUT_S` |

### `em_routes_events`

| Was in em_api | Now |
|---|---|
| `_ws_events` | `em_routes_events._ws_events` |

### `em_routes_link`

| Was in em_api | Now |
|---|---|
| `_get_provision_controller_endpoints` | `em_routes_link._get_provision_controller_endpoints` |
| `_post_pair` | `em_routes_link._post_pair` |
| `_post_provision_tls_credentials` | `em_routes_link._post_provision_tls_credentials` |

### `em_routes_oww`

| Was in em_api | Now |
|---|---|
| `_delete_oww_model` | `em_routes_oww._delete_oww_model` |
| `_get_oww_assets` | `em_routes_oww._get_oww_assets` |
| `_get_oww_models` | `em_routes_oww._get_oww_models` |
| `_post_oww_assets` | `em_routes_oww._post_oww_assets` |
| `_post_oww_model_upload` | `em_routes_oww._post_oww_model_upload` |

### `em_routes_provision`

| Was in em_api | Now |
|---|---|
| `EMOS_IMAGE_FIELDS` | `em_routes_provision.EMOS_IMAGE_FIELDS` |
| `_get_provision_debloat_packages` | `em_routes_provision._get_provision_debloat_packages` |
| `_get_provision_debloat_script` | `em_routes_provision._get_provision_debloat_script` |
| `_get_provision_emos_init` | `em_routes_provision._get_provision_emos_init` |
| `_get_provision_latest_binary` | `em_routes_provision._get_provision_latest_binary` |
| `_get_provision_magisk_db` | `em_routes_provision._get_provision_magisk_db` |
| `_get_provision_oww_asset` | `em_routes_provision._get_provision_oww_asset` |
| `_get_provision_oww_manifest` | `em_routes_provision._get_provision_oww_manifest` |
| `_get_provision_start_script` | `em_routes_provision._get_provision_start_script` |
| `_post_provision_diagnostics` | `em_routes_provision._post_provision_diagnostics` |
| `_post_provision_emos_image` | `em_routes_provision._post_provision_emos_image` |
| `_read_payload` | `em_routes_provision._read_payload` |

### `em_routes_shell`

| Was in em_api | Now |
|---|---|
| `_ws_shell` | `em_routes_shell._ws_shell` |

### `em_routes_static`

| Was in em_api | Now |
|---|---|
| `_bundle_version` | `em_routes_static.bundle_version` |
| `_get_setup_state` | `em_routes_static._get_setup_state` |
| `_post_setup` | `em_routes_static._post_setup` |
| `_redirect_root` | `em_routes_static._redirect_root` |
| `_serve_dashboard` | `em_routes_static._serve_dashboard` |
| `_serve_spa` | `em_routes_static._serve_spa` |
| `STATIC_DIR` | `em_routes_static.STATIC_DIR` |
| `_with_ingress_base` | `em_routes_static._with_ingress_base` |

### `em_routes_system`

| Was in em_api | Now |
|---|---|
| `DEVICE_LOG_LINES` | `em_routes_system.DEVICE_LOG_LINES` |
| `_get_support_bundle` | `em_routes_system._get_support_bundle` |
| `_get_system_config` | `em_routes_system._get_system_config` |
| `_get_system_status` | `em_routes_system._get_system_status` |
| `_patch_system_config` | `em_routes_system._patch_system_config` |

### `em_routes_updates`

| Was in em_api | Now |
|---|---|
| `_get_controller_release` | `em_routes_updates._get_controller_release` |
| `_get_latest_release` | `em_routes_updates._get_latest_release` |
| `_pending_emos_uploads` | `em_routes_updates._pending_emos_uploads` |
| `_pending_uploads` | `em_routes_updates._pending_uploads` |
| `_post_check_release` | `em_routes_updates._post_check_release` |
| `_post_debloat` | `em_routes_updates._post_debloat` |
| `_post_deploy_all` | `em_routes_updates._post_deploy_all` |
| `_post_device_emos_update` | `em_routes_updates._post_device_emos_update` |
| `_post_device_rollback` | `em_routes_updates._post_device_rollback` |
| `_post_device_update` | `em_routes_updates._post_device_update` |
| `_post_emos_upload` | `em_routes_updates._post_emos_upload` |
| `_post_upload_binary` | `em_routes_updates._post_upload_binary` |
| `UPLOAD_MAX_BYTES` | `em_routes_updates.UPLOAD_MAX_BYTES` |

### Removed

| Was in em_api | Now |
|---|---|
| `init` | Gone (#907): em_api reads `em_state` directly, so em_controller no longer hands it references at startup |
| `_devices`, `_shell_pending`, `_shell_dashboard` | Gone (#907): the references `init` set; read `em_state.devices`, `shell_pending`, `shell_dashboard` |
| `_running_controller_module` | Gone (#907): the #306 workaround `em_state` replaces |

## Moved out of em_controller (#907)

| Was in em_controller | Now |
|---|---|
| `_devices` | `em_state.devices` |
| `_shell_pending` / `_shell_dashboard` | `em_state.shell_pending` / `shell_dashboard` |
| `_oww_models` / `_oww_barge_models` | `em_state.oww_models` / `oww_barge_models` |
| `_forget_oww_models` | `em_state.forget_oww_models` |
| `_wake_arbiter` | `em_state.wake_arbiter` |
| `_loop_lag_peak_ms` | `em_state.loop_lag_peak_ms` |

## Still in em_api

`SUPERVISOR_LOG`, `_collect_supervisor_log`, `_error_middleware`, `_ingress_only_middleware`, `create_app`, `create_runner`, `notify_device_connected`, `notify_device_disconnected`, `notify_device_pending`, `notify_pair_request`, `session_prune_loop`.
