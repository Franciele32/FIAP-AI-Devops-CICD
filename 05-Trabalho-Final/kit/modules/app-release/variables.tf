variable "ambiente" {
  description = "Ambiente do pacote (dev ou prod). O raiz passa terraform.workspace."
  type        = string

}

variable "bucket" {
  description = "Bucket base-config-<RM> do setup (o mesmo do backend). O modulo NAO cria bucket."
  type        = string

  validation {
    condition     = startswith(var.bucket, "base-config-")
    error_message = "Use o seu bucket base-config-<RM> (aws s3 ls | grep base-config-), o mesmo do backend.tf."
  }
}
