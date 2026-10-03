output "function_url" {
  description = "The public Function URL. Point the site's Ask widget here."
  value       = aws_lambda_function_url.this.function_url
}

output "function_name" {
  description = "The Lambda function name."
  value       = aws_lambda_function.this.function_name
}

output "cap_table_name" {
  description = "The DynamoDB table holding the per-day question counters."
  value       = aws_dynamodb_table.caps.name
}

output "log_group_name" {
  description = "The CloudWatch log group carrying the turn log."
  value       = aws_cloudwatch_log_group.this.name
}
