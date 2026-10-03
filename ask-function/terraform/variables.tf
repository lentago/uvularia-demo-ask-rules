# ============================================================================
# Variables — everything client-specific, so nothing Lentago-specific is baked in
# ============================================================================
# The demonstration client's values (its SSM path, its OIDC role, its state key)
# are supplied by the lentago/solidago sandbox at deploy time; none of them live
# in this template.

variable "name_prefix" {
  description = "Prefix for every resource name. One deployment per records vault."
  type        = string
  default     = "uvularia"
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

# --- where the box reads from ------------------------------------------------

variable "published_base_url" {
  description = "Base URL of the vault's published branch (GitHub Pages), no trailing slash."
  type        = string
}

variable "rules_repo" {
  description = "The rules repository, as owner/name, whose release the box pins."
  type        = string
}

variable "rules_release_tag" {
  description = "The rules-vN release to pin, or 'latest' to follow the newest within the refresh window."
  type        = string
  default     = "latest"
}

variable "corpus_digest" {
  description = "The corpus digest to serve, or 'latest' to follow the newest published corpus."
  type        = string
  default     = "latest"
}

variable "obligations_url" {
  description = "Optional URL of an obligations JSON list so a breach maps to subjects, not bare ids."
  type        = string
  default     = ""
}

variable "refresh_seconds" {
  description = "How long a fetched corpus/rules snapshot is served before re-polling."
  type        = number
  default     = 300
}

# --- the guards --------------------------------------------------------------

variable "daily_cap" {
  description = "Fallback hard ceiling on questions per UTC day. policy.yaml's daily_cap overrides it when present."
  type        = number
  default     = 500
}

variable "allowed_origin" {
  description = "The one site origin allowed to call the box (e.g. https://org.github.io). '*' disables the check."
  type        = string
}

variable "anthropic_api_key_ssm_path" {
  description = "Path of the SSM SecureString holding the Anthropic API key. Created out of band so the key never enters Terraform state."
  type        = string
}

variable "turnstile_enabled" {
  description = "Require a Cloudflare Turnstile token. Off by default for local and CI runs."
  type        = bool
  default     = false
}

variable "turnstile_secret_ssm_path" {
  description = "Path of the SSM SecureString holding the Turnstile secret. Required only when turnstile_enabled."
  type        = string
  default     = ""
}

variable "maintenance_message" {
  description = "Reply returned when policy.yaml says enabled: false."
  type        = string
  default     = "This records assistant is paused for maintenance. The published records and the board are still available."
}

# --- the role ----------------------------------------------------------------

variable "permissions_boundary" {
  description = "Optional IAM permissions-boundary ARN applied to the function role (set by orgs that mandate one)."
  type        = string
  default     = null
}

# --- sizing and retention ----------------------------------------------------

variable "lambda_timeout" {
  description = "Function timeout in seconds (bounds a slow model call)."
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Function memory in MB."
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the function's log group."
  type        = number
  default     = 30
}

variable "dynamodb_read_capacity" {
  description = "Provisioned read units for the cap table (1 stays inside the always-free tier)."
  type        = number
  default     = 1
}

variable "dynamodb_write_capacity" {
  description = "Provisioned write units for the cap table (1 stays inside the always-free tier)."
  type        = number
  default     = 1
}
