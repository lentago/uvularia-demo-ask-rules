# Pinned for the function-URL permission model: a NONE-auth URL created on or
# after October 2025 needs an explicit lambda:InvokeFunction grant in addition to
# lambda:InvokeFunctionUrl (see main.tf), which the 5.x provider expresses.
terraform {
  required_version = ">= 1.4"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40, < 6.0"
    }
    null = {
      source  = "hashicorp/null"
      version = ">= 3.0"
    }
  }
}
