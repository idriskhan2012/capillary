// Runtime config for the frontend. This committed copy is a placeholder for local dev
// (the deep-dive generator button is disabled when deepDiveApiUrl is null). On deploy,
// infra/deploy.sh overwrites the S3 copy with the live API Gateway URL.
window.CAPILLARY_CONFIG = { deepDiveApiUrl: null };
