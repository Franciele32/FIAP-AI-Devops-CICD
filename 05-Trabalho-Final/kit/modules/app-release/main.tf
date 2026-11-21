# Modulo do kit (PRONTO, nao precisa mexer): empacota a pasta app/ da raiz do
# repositorio e publica o pacote no S3, um por ambiente.
#
# Por que o release_id vem do CONTEUDO dos arquivos (e nao do md5 do zip): o git
# clone do runner muda a data de cada arquivo, e o zip guarda essa data. Se o
# release dependesse do zip, todo pipeline criaria um release "novo" e trocaria
# as EC2 sem nenhuma mudanca no app. Com o hash do conteudo, so muda o release
# quando muda codigo, corpus ou configuracao do assistente.

locals {
  app_dir = "${path.root}/app"
  # Caches do Python nao fazem parte do app: ficam fora do hash e do zip.
  app_files = sort([
    for f in fileset(local.app_dir, "**") : f
    if !strcontains(f, "__pycache__") && !strcontains(f, ".pytest_cache") && !endswith(f, ".pyc")
  ])
  content_md5 = md5(join("\n", [for f in local.app_files : "${f}:${filemd5("${local.app_dir}/${f}")}"]))
  release_id  = substr(local.content_md5, 0, 12)
  bundle_key  = "trabalho-final-ai/releases/${var.ambiente}/app-${local.content_md5}.zip"
}

# O zip e gerado ja no plan, dentro de build/. O job plan-<env> publica build/
# como artefato junto do plan-<env>.tfplan, e o deploy-<env> envia esse mesmo zip.
data "archive_file" "app" {
  type        = "zip"
  source_dir  = local.app_dir
  output_path = "${path.root}/build/app-${var.ambiente}.zip"
  excludes = [
    for f in fileset(local.app_dir, "**") : f
    if strcontains(f, "__pycache__") || strcontains(f, ".pytest_cache") || endswith(f, ".pyc")
  ]
}

resource "aws_s3_object" "bundle" {
  bucket = var.bucket
  key    = local.bundle_key
  source = data.archive_file.app.output_path
  # source_hash (e nao etag do zip): reenvia so quando o CONTEUDO de app/ muda.
  # O md5 do zip muda a cada git clone (datas dos arquivos) e deixaria todo plan
  # com uma alteracao falsa neste objeto.
  source_hash  = local.content_md5
  content_type = "application/zip"

  lifecycle {
    precondition {
      condition     = contains(["dev", "prod"], var.ambiente)
      error_message = "O ambiente precisa ser dev ou prod: selecione o workspace antes do plan (terraform workspace select dev || terraform workspace new dev)."
    }
  }

  tags = {
    Ambiente  = var.ambiente
    ReleaseId = local.release_id
  }
}
