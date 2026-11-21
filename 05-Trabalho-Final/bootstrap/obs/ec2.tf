# EC2 com OTel Collector, Prometheus, Loki, Jaeger e Grafana em Docker Compose,
# o mesmo desenho da Aula 5 (04-GenAIOps/quantum-assistant/ops/cloud).
resource "aws_instance" "obs" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.obs.id]
  associate_public_ip_address = true
  # LabInstanceProfile: a unica permitida no Learner Lab; da SSM (sem SSH) e S3.
  iam_instance_profile = "LabInstanceProfile"

  # IMDSv2 com hop limit 1: um conteiner do compose nao alcanca as credenciais
  # da instancia. Quem le o SSM (senha do Grafana) e o bootstrap no host.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 16
    encrypted             = true
    delete_on_termination = true
  }

  # O user_data so instala a AWS CLI e chama o bootstrap.sh do S3. Mudar a
  # configuracao do compose NAO recria a EC2: o make bootstrap reenvia os
  # arquivos e roda o bootstrap.sh de novo via SSM.
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    config_bucket = var.config_bucket
    config_prefix = "trabalho-final-ai/obs-config"
    region        = var.aws_region
  })
  user_data_replace_on_change = false

  tags = { Name = local.name }

  lifecycle {
    # Uma AMI nova da Canonical nao deve derrubar a observabilidade no meio do trabalho.
    ignore_changes = [ami]
  }
}
