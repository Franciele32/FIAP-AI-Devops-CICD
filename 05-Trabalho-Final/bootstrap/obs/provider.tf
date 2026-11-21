provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "quantum-commerce"
      ManagedBy = "terraform"
      Lab       = "trabalho-final-ai"
    }
  }
}
