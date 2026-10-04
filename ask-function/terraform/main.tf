# ============================================================================
# The Ask function: mitchella's engine as a Lambda behind a public Function URL,
# in the CLIENT's AWS account. Hardened per docs/concept.md §5 and the issue:
#
#   * the Anthropic key is read from an SSM SecureString at cold start (a data
#     source here only grants the role access to it — the value never enters
#     Terraform state);
#   * the daily cap is a durable DynamoDB counter, not per-container memory;
#   * the log group is pre-created with retention, so the role needs no
#     CreateLogGroup;
#   * the role is least privilege and accepts an optional permissions boundary;
#   * CORS and the bot check live in the handler (one authority for the headers).
#
# Lineage: solidago's modules/ask-lambda, with its plaintext key and in-memory
# cap replaced. Nothing Lentago-specific is here; every account-specific value is
# a variable.
# ============================================================================

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  name          = "${var.name_prefix}-ask"
  log_group     = "/aws/lambda/${local.name}"
  log_group_arn = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:${local.log_group}"
  package       = "${path.module}/build/package.zip"

  # The function's source files. Their combined hash is the Lambda's
  # source_code_hash, so a code or dependency change redeploys without the data
  # source needing the (apply-time) zip to exist at plan.
  src_files = fileset("${path.module}/../src", "**/*.py")
  src_hash = sha1(join("", concat(
    [for f in sort(local.src_files) : filesha1("${path.module}/../src/${f}")],
    [filesha1("${path.module}/../requirements.txt")],
  )))
}

# --- Package the function (source + pinned deps) -----------------------------
# mitchella and anthropic are not in the Lambda runtime, so the package is built
# by build.sh (pip install --target, then zip). boto3 IS in the runtime and is
# not vendored. This null_resource is a convenience for LOCAL applies only: it
# re-runs when a source file or requirements.txt changes (trigger: local.src_hash),
# which means a fresh CI runner with unchanged sources would have no zip. The
# deploy workflow therefore runs build.sh itself before every plan and apply.
resource "null_resource" "build" {
  triggers = {
    src_hash = local.src_hash
  }

  provisioner "local-exec" {
    command = "${path.module}/build.sh"
  }
}

# --- Execution role (least privilege) ----------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = local.name
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary
  tags                 = var.tags
}

data "aws_iam_policy_document" "this" {
  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${local.log_group_arn}:*"]
  }

  statement {
    sid       = "DailyCapCounter"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.caps.arn]
  }

  statement {
    sid     = "ReadTheApiKey"
    actions = ["ssm:GetParameter"]
    resources = concat(
      [local.anthropic_key_param_arn],
      var.turnstile_enabled ? [local.turnstile_param_arn] : [],
    )
  }

  # The SecureStrings are KMS-encrypted. Scope decrypt to calls made via SSM in
  # this region rather than naming a key arn, which covers both the default
  # aws/ssm key and a customer-managed one.
  statement {
    sid       = "DecryptViaSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.name}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "${local.name}-policy"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.this.json
}

# The parameters are referenced by ARN, never read: the maintainer creates them
# out of band (see README) and the function reads them at cold start. Nothing
# about the key touches Terraform state, and the first apply does not need the
# parameter to exist yet — the box simply serves its maintenance line until it does.
locals {
  ssm_parameter_arn_prefix = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter"
  anthropic_key_param_arn  = "${local.ssm_parameter_arn_prefix}${var.anthropic_api_key_ssm_path}"
  turnstile_param_arn      = var.turnstile_enabled ? "${local.ssm_parameter_arn_prefix}${var.turnstile_secret_ssm_path}" : null
}

# --- The durable daily cap ----------------------------------------------------
# One item per UTC day; a TTL on `expires` lets DynamoDB delete old counters for
# free. Provisioned 1/1 keeps the table inside the always-free 25-unit tier.
resource "aws_dynamodb_table" "caps" {
  name           = "${local.name}-caps"
  billing_mode   = "PROVISIONED"
  read_capacity  = var.dynamodb_read_capacity
  write_capacity = var.dynamodb_write_capacity
  hash_key       = "day"

  attribute {
    name = "day"
    type = "S"
  }

  ttl {
    attribute_name = "expires"
    enabled        = true
  }

  tags = var.tags
}

# --- Log group (pre-created with retention) ----------------------------------
resource "aws_cloudwatch_log_group" "this" {
  name              = local.log_group
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# --- The function ------------------------------------------------------------
resource "aws_lambda_function" "this" {
  function_name = local.name
  role          = aws_iam_role.this.arn
  runtime       = "python3.12"
  handler       = "handler.handler"

  filename         = local.package
  source_code_hash = local.src_hash

  timeout     = var.lambda_timeout
  memory_size = var.lambda_memory_size

  environment {
    variables = {
      UVULARIA_PUBLISHED_BASE            = var.published_base_url
      UVULARIA_CORPUS_DIGEST             = var.corpus_digest
      UVULARIA_RULES_REPO                = var.rules_repo
      UVULARIA_RULES_TAG                 = var.rules_release_tag
      UVULARIA_DAILY_CAP                 = tostring(var.daily_cap)
      UVULARIA_ALLOWED_ORIGIN            = var.allowed_origin
      UVULARIA_CAP_TABLE                 = aws_dynamodb_table.caps.name
      UVULARIA_SSM_KEY_PATH              = var.anthropic_api_key_ssm_path
      UVULARIA_TURNSTILE_ENABLED         = var.turnstile_enabled ? "1" : "0"
      UVULARIA_TURNSTILE_SECRET_SSM_PATH = var.turnstile_secret_ssm_path
      UVULARIA_REFRESH_SECONDS           = tostring(var.refresh_seconds)
      UVULARIA_MAINTENANCE_MESSAGE       = var.maintenance_message
      UVULARIA_OBLIGATIONS_URL           = var.obligations_url
    }
  }

  depends_on = [
    null_resource.build,
    aws_iam_role_policy.this,
    aws_cloudwatch_log_group.this,
  ]

  tags = var.tags
}

# --- Public Function URL -----------------------------------------------------
# auth NONE by design (a public ask box). Abuse is bounded by the handler's
# origin check, the durable daily cap, and the Anthropic key's own spend cap.
# No `cors {}` block here: the handler owns the CORS headers, and setting both
# makes the browser reject a duplicated Access-Control-Allow-Origin header.
resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "NONE"
}

# A NONE-auth URL created on/after October 2025 needs BOTH grants or every
# anonymous call 403s even though the function is healthy.
resource "aws_lambda_permission" "url" {
  statement_id           = "AllowPublicInvokeFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke_function" {
  statement_id  = "AllowPublicInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "*"
}
