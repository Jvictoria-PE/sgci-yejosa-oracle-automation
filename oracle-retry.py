#!/usr/bin/env python3
"""Fail-closed Oracle Always Free provisioning retry, using an approved Terraform plan."""
import os
import re
import tempfile
from pathlib import Path

import oci

REGION = "sa-saopaulo-1"
STACK_NAME = "sgci-yejosa-always-free"
APPROVED_PLAN = "ocid1.ormjob.oc1.sa-saopaulo-1.amaaaaaan4ylbvaalcbw32byrfzxn2izwsezwy2fotjctnqvesui5vwpyb6a"
ACTIVE = {"ACCEPTED", "IN_PROGRESS", "CANCELING"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED"}
CAPACITY_ERROR = re.compile(r"out.of.host.capacity|outofhostcapacity|out.of.capacity", re.I)

def all_pages(call, **kwargs):
    results, page = [], None
    for _ in range(25):
        response = call(**kwargs, **({"page": page} if page else {}))
        results.extend(response.data)
        page = response.headers.get("opc-next-page")
        if not page:
            return results
    raise RuntimeError("Exceeded pagination safety limit; refusing to apply")

def main():
    for name in ("OCI_USER_OCID", "OCI_TENANCY_OCID", "OCI_FINGERPRINT",
                 "OCI_PRIVATE_KEY", "OCI_STACK_OCID"):
        if not os.environ.get(name):
            raise RuntimeError(f"Missing required secret: {name}")

    with tempfile.TemporaryDirectory() as directory:
        key_file = Path(directory) / "key.pem"
        key_file.write_text(os.environ["OCI_PRIVATE_KEY"].rstrip("\n") + "\n")
        key_file.chmod(0o600)
        config = {
            "user": os.environ["OCI_USER_OCID"],
            "tenancy": os.environ["OCI_TENANCY_OCID"],
            "fingerprint": os.environ["OCI_FINGERPRINT"],
            "key_file": str(key_file),
            "region": REGION,
        }
        oci.config.validate_config(config)
        manager = oci.resource_manager.ResourceManagerClient(config)
        compute = oci.core.ComputeClient(config)
        block = oci.core.BlockstorageClient(config)
        stack = manager.get_stack(os.environ["OCI_STACK_OCID"]).data

        if stack.display_name != STACK_NAME or stack.compartment_id != config["tenancy"]:
            raise RuntimeError("Stack identity/compartment mismatch; no apply")
        if stack.lifecycle_state != "ACTIVE":
            raise RuntimeError("Stack is not ACTIVE; no apply")

        jobs = all_pages(manager.list_jobs, stack_id=stack.id, sort_by="TIMECREATED",
                         sort_order="DESC", limit=100)
        if any(job.lifecycle_state not in ACTIVE | TERMINAL for job in jobs):
            raise RuntimeError("Unknown job lifecycle state; no apply")
        if any(job.lifecycle_state in ACTIVE for job in jobs):
            print("Oracle has an active job; skipping this cycle.")
            return

        instances = all_pages(compute.list_instances, compartment_id=config["tenancy"], limit=100)
        if any(instance.lifecycle_state != "TERMINATED" for instance in instances):
            print("Compute instance already exists; no more APPLY attempts.")
            return

        # Protect against a partial allocation if an earlier APPLY stopped unexpectedly.
        availability_domains = oci.identity.IdentityClient(config).list_availability_domains(
            compartment_id=config["tenancy"]).data
        for domain in availability_domains:
            volumes = all_pages(block.list_boot_volumes, availability_domain=domain.name,
                                compartment_id=config["tenancy"], limit=100)
            if any(volume.lifecycle_state not in {"TERMINATED", "TERMINATING"} for volume in volumes):
                print("Boot volume already exists; stop to avoid duplication.")
                return
        volumes = all_pages(block.list_volumes, compartment_id=config["tenancy"], limit=100)
        if any(volume.lifecycle_state not in {"TERMINATED", "TERMINATING"} for volume in volumes):
            print("Block volume already exists; stop to avoid duplication.")
            return

        plans = [job for job in jobs if job.operation == "PLAN"]
        if not plans or plans[0].id != APPROVED_PLAN or plans[0].lifecycle_state != "SUCCEEDED":
            raise RuntimeError("Approved plan is not the latest successful plan; no apply")

        plan_job = manager.get_job(APPROVED_PLAN).data
        if plan_job.stack_id != stack.id or plan_job.lifecycle_state != "SUCCEEDED":
            raise RuntimeError("Approved plan identity/status mismatch; no apply")

        # This exact plan was reviewed: one Always Free A1 VM, one OCPU, six GiB,
        # 50 GiB boot volume, no updates or destroys. Never use AUTO_APPROVED.
        applies = [job for job in jobs if job.operation == "APPLY"]
        if applies:
            last = applies[0]
            if last.lifecycle_state == "SUCCEEDED":
                print("An APPLY previously succeeded; manual investigation required before any retry.")
                return
            if last.lifecycle_state != "FAILED":
                print("Last APPLY was not a capacity failure; no new retry.")
                return

            failure = manager.get_job(last.id).data
            detail = getattr(failure, "failure_details", None)
            fragments = [str(getattr(detail, "message", "") or "")]
            try:
                log_response = manager.get_job_logs_content(last.id)
                log_body = log_response.data
                if hasattr(log_body, "content"):
                    log_body = log_body.content
                if hasattr(log_body, "decode"):
                    log_body = log_body.decode("utf-8", errors="replace")
                fragments.append(str(log_body)[-500000:])
            except Exception:
                # Do not assume capacity failure if evidence could not be retrieved.
                print("Unable to retrieve failure logs; no retry.")
                return
            error_text = "\n".join(fragments)
            if not CAPACITY_ERROR.search(error_text):
                print("Last APPLY did not show a recognized host-capacity error; halt retries.")
                return
            print("Previous APPLY failed because of host capacity; retry permitted.")
        else:
            print("No prior APPLY found; attempting reviewed plan.")

        details = oci.resource_manager.models.CreateJobDetails(
            stack_id=stack.id,
            display_name="sgci-yejosa-capacity-retry",
            operation="APPLY",
            job_operation_details=oci.resource_manager.models.CreateApplyJobOperationDetails(
                operation="APPLY",
                execution_plan_strategy="FROM_PLAN_JOB_ID",
                execution_plan_job_id=APPROVED_PLAN,
                is_provider_upgrade_required=False,
            ),
        )
        created = manager.create_job(create_job_details=details).data
        print("Submitted Terraform APPLY using fixed approved plan.")
        print("Job ID:", created.id)
        print("Initial state:", created.lifecycle_state)
        print("No automatic plan regeneration or billable service enabled.")

if __name__ == "__main__":
    main()
