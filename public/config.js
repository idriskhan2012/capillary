// Overwritten at deploy time (infra/deploy.sh) with the live Add-Stock API base URL.
// When unset (local dev), the app falls back to browser localStorage for custom stocks.
window.CAPILLARY_API_BASE = window.CAPILLARY_API_BASE || null;
