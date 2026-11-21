output "instance_id" {
  value = aws_instance.obs.id
}

output "private_ip" {
  description = "IP privado: o app manda OTLP para <ip>:4318."
  value       = aws_instance.obs.private_ip
}

output "otlp_endpoint" {
  value = "http://${aws_instance.obs.private_ip}:4318"
}

output "obs_query" {
  description = "Enderecos de consulta (so o runner alcanca), em JSON, gravados no SSM obs-query."
  value = jsonencode({
    prometheus = "http://${aws_instance.obs.private_ip}:9090"
    loki       = "http://${aws_instance.obs.private_ip}:3100"
    jaeger     = "http://${aws_instance.obs.private_ip}:16686"
  })
}

output "security_group_id" {
  value = aws_security_group.obs.id
}
