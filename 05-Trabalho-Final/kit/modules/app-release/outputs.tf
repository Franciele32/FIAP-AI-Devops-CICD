output "bundle_uri" {
  description = "Endereco s3:// do pacote do app; o user_data baixa daqui no primeiro boot."
  value       = "s3://${aws_s3_object.bundle.bucket}/${aws_s3_object.bundle.key}"
}

output "release_id" {
  description = "Identificador curto do release (hash do conteudo de app/). Aparece no /health."
  value       = local.release_id
}
