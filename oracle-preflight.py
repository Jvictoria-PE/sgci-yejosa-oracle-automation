#!/usr/bin/env python3
"""Read-only Oracle preflight. No resource creation or modification."""
import os
import tempfile
from pathlib import Path

import oci

REGION = "sa-saopaulo-1"
EXPECTED_STACK = "sgci-yejosa-always-free"
EXPECTED_PLAN_ID = "ocid1.ormjob.oc1.sa-saopaulo-1.amaaaaaan4ylbvaalcbw32byrfzxn2izwsezwy2fotjctnqvesui5vwpyb6a"
ACTIVE_STATES = {"ACCEPTED", "IN_PROGRESS", "CANCELING"}
TERMINAL_STATES = {"FAILED", "SUCCEEDED", "CANCELED"}


def pages(request, **kwargs):
    items = []
    token = None
    for _ in range(25):
        response = request(**kwargs, **({"page": token} if token else {}))
        items.extend(response.data)
        token = response.headers.get("opc-next-page")
        if not token:
            return items
    raise RuntimeError("Too many pages; refusing to infer safety from partial results")


def main():
    for name in ("OCI_USER_OCID", "OCI_TENANCY_OCID", "OCI_FINGERPRINT",
                 "OCI_PRIVATE_KEY", "OCI_STACK_OCID"):
        if not os.environ.get(name):
            raise RuntimeError("Missing secret: " + name)

    with tempfile.TemporaryDirectory() as directory:
        key_path = Path(directory) / "key.pem"
        key_path.write_text(os.environ["OCI_PRIVATE_KEY"].rstrip("\n") + "\n")
        key_path.chmod(0o600)
        config = {
            "user": os.environ["OCI_USER_OCID"],
            "tenancy": os.environ["OCI_TENANCY_OCID"],
            "fingerprint": os.environ["OCI_FINGERPRINT"],
            "key_file": str(key_path),
            "region": REGION,
        }
        oci.config.validate_config(config)
        manager = oci.resource_manager.ResourceManagerClient(config)
        compute = oci.core.ComputeClient(config)
        stack = manager.get_stack(os.environ["OCI_STACK_OCID"]).data
        if stack.display_name != EXPECTED_STACK or stack.compartment_id != config["tenancy"]:
            raise RuntimeError("Unexpected stack identity or compartment")

        jobs = pages(manager.list_jobs, stack_id=stack.id, sort_by="TIMECREATED",
                     sort_order="DESC", limit=100)
        instances = pages(compute.list_instances, compartment_id=config["tenancy"], limit=100)
        relevant = [v for v in instances if v.lifecycle_state != "TERMINATED"]
        active = [j for j in jobs if j.lifecycle_state in ACTIVE_STATES]
        unknown = [j for j in jobs if j.lifecycle_state not in (ACTIVE_STATES | TERMINAL_STATES)]
        applies = [j for j in jobs if j.operation == "APPLY"]

        print("Region:", REGION)
        print("Stack name:", stack.display_name)
        print("Stack lifecycle:", stack.lifecycle_state)
        print("Total OCI jobs:", len(jobs))
        print("Active jobs:", len(active))
        print("Non-terminated compute instances:", len(relevant))
        print("Latest APPLY:", applies[0].lifecycle_state if applies else "none")
        if jobs:
            print("Latest job type/state:", jobs[0].operation, jobs[0].lifecycle_state)
        expected_plan = next((j for j in jobs if j.id == EXPECTED_PLAN_ID), None)
        print("Previously approved PLAN:", expected_plan.lifecycle_state if expected_plan else "not listed")
        print("No modifications performed.")
        if unknown:
            raise RuntimeError("Unknown Oracle job state; manual review required")
        if active:
            print("Preflight: Oracle job running; do not start another APPLY")
        elif relevant:
            print("Preflight: Compute already exists; do not start another APPLY")
        elif applies and applies[0].lifecycle_state == "SUCCEEDED":
            print("Preflight: Previous APPLY succeeded but no VM found; manual review required")
        elif expected_plan is None or expected_plan.lifecycle_state != "SUCCEEDED":
            print("Preflight: Approved PLAN not confirmed; manual review required")
        else:
            print("Preflight: No running jobs or compute instances; further guard checks needed before APPLY")


if __name__ == "__main__":
    main()
