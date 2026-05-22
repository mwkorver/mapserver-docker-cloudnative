# IAM Roles Provisioning Guide

To support a clean separation of concerns and robust `cdk destroy` cycles, the IAM roles for the Fargate Task and Task Execution are managed out-of-band. They must be created in your AWS account before deploying the main CDK infrastructure.

These roles are long-lived and will remain intact even when the transient application stack is created, updated, or destroyed.

---

## 1. Create the Task Execution Role

The **Task Execution Role** is used by the ECS container agent to authenticate with ECR (to pull the MapServer Docker image) and with CloudWatch (to stream logs).

### Step 1.1: Create the Role with Trust Policy
Create the role and allow the ECS Task service principal to assume it:
```bash
aws iam create-role \
  --role-name MapserverFargateExecutionRole \
  --assume-role-policy-document file://task-role-trust-policy.json
```

### Step 1.2: Attach Permissions Policy
Attach the custom task execution policy to the role:
```bash
aws iam put-role-policy \
  --role-name MapserverFargateExecutionRole \
  --policy-name MapserverFargateExecutionPolicy \
  --policy-document file://task-execution-role-policy.json
```

---

## 2. Create the Task Role

The **Task Role** is the identity assumed by the MapServer proxy and scanner running *inside* the container. It enables reading and writing S3 collections metadata, and signing S3 range requests for serving COGs.

### Step 2.1: Create the Role with Trust Policy
```bash
aws iam create-role \
  --role-name MapserverFargateTaskRole \
  --assume-role-policy-document file://task-role-trust-policy.json
```

### Step 2.2: Attach Permissions Policy
Attach the S3 config and imagery access permissions policy:
```bash
aws iam put-role-policy \
  --role-name MapserverFargateTaskRole \
  --policy-name MapserverFargateTaskPolicy \
  --policy-document file://task-role-policy.json
```

---

## 3. Retrieve the Role ARNs

Verify that both roles were created successfully and grab their ARNs:
```bash
aws iam get-role --role-name MapserverFargateTaskRole --query "Role.Arn" --output text
aws iam get-role --role-name MapserverFargateExecutionRole --query "Role.Arn" --output text
```

You will pass these ARNs to your `cdk deploy` command:
```bash
npx cdk deploy \
  -c task_role_arn=arn:aws:iam::123456789012:role/MapserverFargateTaskRole \
  -c execution_role_arn=arn:aws:iam::123456789012:role/MapserverFargateExecutionRole
```
