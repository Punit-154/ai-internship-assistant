

A production-style auto-scaling architecture built entirely via AWS CLI — 
Flask + gunicorn behind an Application Load Balancer, with an Auto Scaling 
Group that reacts to real CloudWatch metrics.


## Live demo
<img width="1600" height="373" alt="image" src="https://github.com/user-attachments/assets/ade41a81-02fe-4a04-9f17-bbd4a6f8dcc0" />
<img width="1600" height="567" alt="image" src="https://github.com/user-attachments/assets/b2dc3daa-b267-4371-adb0-1890929bad85" />
 — instance ID and Availability Zone change on 
every refresh as the ALB load-balances across the fleet.

## Architecture
<img width="1312" height="1050" alt="image" src="https://github.com/user-attachments/assets/f090fdeb-ee3f-4396-9771-7e09184fb020" />


- VPC (10.0.0.0/16) — public + private subnets across 2 AZs
- Application Load Balancer — Layer 7 routing, health checks on /health
- Auto Scaling Group — min 2 / max 6, scales on CloudWatch CPU alarms
- Golden AMI strategy — no NAT Gateway, ~40% cost reduction
- IAM least-privilege role — CloudWatch + SSM only, no broad EC2 access

## What it does
- `/` — live HTML dashboard showing instance ID, AZ, uptime, request count
- `/load` — deliberately burns CPU to trigger a live scale-out demo
- `/health` — ALB health check endpoint

## Proof it scales
<img width="1600" height="785" alt="image" src="https://github.com/user-attachments/assets/d3614097-fd8c-4cac-b555-64a70c602d51" />
— alarm firing under load

## Key engineering decisions
- Skipped the NAT Gateway (~$35/month saved) by pre-baking all dependencies 
  into a custom AMI instead of installing at boot
- Asymmetric scaling policy: +2 instances aggressively on spike, -1 
  conservatively on cooldown, to avoid flapping
- EC2 instances sit in private subnets, reachable only via ALB security 
  group reference (not CIDR-based)

## Debugging notes (the real learning)
- CloudWatch queries must use UTC — local time silently returns zero data
- t3.micro burst credits exhaust under sustained testing, hard-throttling 
  CPU at 20% baseline
- PowerShell's `Out-File -Encoding utf8` writes a BOM that breaks AWS CLI 
  JSON parsing — use `[System.IO.File]::WriteAllText()` instead

## Tech stack
Python 3.11, Flask 3.0, gunicorn, Amazon Linux 2023, AWS CLI v2, PowerShell

## Rebuild it yourself
See `scripts/rebuild.md` for the full CLI command sequence.
