# route53

Private hosted zone for RDS internal DNS, plus a public hosted zone with active-passive failover routing (primary/secondary ALB, optional CloudFront). See [../../docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md#terraform-module-graph).


<!-- BEGIN_TF_DOCS -->
## Requirements

No requirements.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_route53_health_check.primary](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_health_check) | resource |
| [aws_route53_record.api](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_record.frontend](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_record.primary](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_record.primary_cf](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_record.rds_endpoint](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_record.secondary](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_record) | resource |
| [aws_route53_zone.rds_private](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/route53_zone) | resource |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_cloudfront_domain"></a> [cloudfront\_domain](#input\_cloudfront\_domain) | CloudFront distribution domain name. Required when enable\_cloudfront=true. | `string` | `""` | no |
| <a name="input_domain"></a> [domain](#input\_domain) | Apex domain for the public hosted zone and failover records (e.g. example.com) | `string` | n/a | yes |
| <a name="input_enable_cloudfront"></a> [enable\_cloudfront](#input\_enable\_cloudfront) | When true, the primary record points at cloudfront\_domain instead of primary\_alb\_dns. | `bool` | `false` | no |
| <a name="input_primary_alb_dns"></a> [primary\_alb\_dns](#input\_primary\_alb\_dns) | Nginx NLB DNS in primary region. Leave empty to skip app records before EKS is ready. | `string` | `""` | no |
| <a name="input_rds_endpoint"></a> [rds\_endpoint](#input\_rds\_endpoint) | RDS instance endpoint to create a CNAME record for | `string` | n/a | yes |
| <a name="input_rds_private_zone_name"></a> [rds\_private\_zone\_name](#input\_rds\_private\_zone\_name) | Name of the private Route 53 hosted zone for internal RDS DNS | `string` | `"bookstore.internal"` | no |
| <a name="input_rds_record_name"></a> [rds\_record\_name](#input\_rds\_record\_name) | DNS record name for the RDS endpoint within the private zone | `string` | `"db.bookstore.internal"` | no |
| <a name="input_secondary_alb_dns"></a> [secondary\_alb\_dns](#input\_secondary\_alb\_dns) | Nginx NLB DNS in secondary region. Fill after secondary EKS deploy. | `string` | `""` | no |
| <a name="input_vpc_id"></a> [vpc\_id](#input\_vpc\_id) | VPC ID for the private hosted zone association | `string` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_public_name_servers"></a> [public\_name\_servers](#output\_public\_name\_servers) | NS records already set at your domain registrar by scripts/init-domain.sh — informational only |
| <a name="output_public_zone_id"></a> [public\_zone\_id](#output\_public\_zone\_id) | Route53 public hosted zone ID — created once via scripts/init-domain.sh, looked up here |
<!-- END_TF_DOCS -->