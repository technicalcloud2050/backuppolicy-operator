import os
import logging
from datetime import datetime, timezone

import kopf
from croniter import croniter
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

LOG = logging.getLogger(__name__)
GROUP, VERSION, PLURAL = 'ops.example.com', 'v1alpha1', 'backuppolicies'


def validate(spec):
    schedule = spec.get('schedule')
    if not isinstance(schedule, str) or len(schedule.split()) != 5 or not croniter.is_valid(schedule):
        raise ValueError('schedule must be a valid five-field cron expression')
    target = spec.get('target') or {}
    if target.get('kind') != 'PersistentVolumeClaim' or not target.get('name'):
        raise ValueError('target must reference a named PersistentVolumeClaim')
    retention = spec.get('retention', 24)
    if type(retention) is not int or retention < 1:
        raise ValueError('retention must be a positive integer')
    if type(spec.get('suspend', False)) is not bool:
        raise ValueError('suspend must be boolean')
    return schedule, target['name'], retention, spec.get('suspend', False)


def desired_cronjob(name, namespace, uid, schedule, pvc, suspend):
    # This is deliberately a simulation; no real PVC data is read or copied.
    return {
        'apiVersion': 'batch/v1', 'kind': 'CronJob',
        'metadata': {'name': name, 'namespace': namespace,
                     'labels': {'app.kubernetes.io/managed-by': 'backuppolicy-operator'},
                     'ownerReferences': [{'apiVersion': f'{GROUP}/{VERSION}', 'kind': 'BackupPolicy',
                                          'name': name, 'uid': uid, 'controller': True, 'blockOwnerDeletion': True}]},
        'spec': {'schedule': schedule, 'suspend': suspend, 'concurrencyPolicy': 'Forbid',
                 'startingDeadlineSeconds': 180,
                 'successfulJobsHistoryLimit': 3, 'failedJobsHistoryLimit': 3,
                 'jobTemplate': {'spec': {'backoffLimit': 2, 'activeDeadlineSeconds': 300,
                   'template': {'spec': {'restartPolicy': 'Never', 'containers': [{
                     'name': 'backup-simulator', 'image': 'busybox:1.36',
                     'command': ['sh', '-c', 'echo "Simulating backup for PVC $TARGET_PVC"; sleep 2; echo "Backup simulation successful"'],
                     'env': [{'name': 'TARGET_PVC', 'value': pvc}],
                     'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'},
                                   'limits': {'cpu': '100m', 'memory': '64Mi'}},
                     'securityContext': {'allowPrivilegeEscalation': False,
                                         'capabilities': {'drop': ['ALL']},
                                         'runAsNonRoot': True, 'runAsUser': 10001,
                                         'seccompProfile': {'type': 'RuntimeDefault'}}
                   }]}}}}}
    }


def condition(ready, reason, message):
    return {'type': 'Ready', 'status': 'True' if ready else 'False',
            'reason': reason, 'message': message[:500],
            'lastTransitionTime': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


def patch_status(api, namespace, name, generation, ready, reason, message, **extra):
    status = {'observedGeneration': generation, 'conditions': [condition(ready, reason, message)], **extra}
    api.patch_namespaced_custom_object_status(GROUP, VERSION, namespace, PLURAL, name, {'status': status})


def reconcile(spec, name, namespace, uid, generation):
    custom = client.CustomObjectsApi()
    core = client.CoreV1Api()
    batch = client.BatchV1Api()
    try:
        schedule, pvc, retention, suspend = validate(spec)
    except ValueError as exc:
        patch_status(custom, namespace, name, generation, False, 'InvalidSpec', str(exc))
        return

    try:
        core.read_namespaced_persistent_volume_claim(pvc, namespace)
    except ApiException as exc:
        if exc.status == 404:
            # Suspend an existing CronJob to avoid launching work against a missing PVC.
            try:
                batch.patch_namespaced_cron_job(name, namespace, {'spec': {'suspend': True}})
            except ApiException as patch_exc:
                if patch_exc.status != 404:
                    raise
            patch_status(custom, namespace, name, generation, False, 'TargetNotFound', f'PVC {pvc} does not exist')
            return
        raise

    desired = desired_cronjob(name, namespace, uid, schedule, pvc, suspend)
    try:
        existing = batch.read_namespaced_cron_job(name, namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        existing = None

    if existing is None:
        batch.create_namespaced_cron_job(namespace, desired)
        LOG.info('Created CronJob %s/%s', namespace, name)
    else:
        owners = existing.metadata.owner_references or []
        if not any(owner.uid == uid and owner.controller for owner in owners):
            patch_status(custom, namespace, name, generation, False, 'OwnershipConflict', 'CronJob exists but is not owned by this BackupPolicy')
            return
        current = existing.spec
        container = current.job_template.spec.template.spec.containers[0]
        current_pvc = next((e.value for e in (container.env or []) if e.name == 'TARGET_PVC'), None)
        if (current.schedule != schedule or current.suspend != suspend or current_pvc != pvc):
            batch.patch_namespaced_cron_job(name, namespace, {'spec': desired['spec']})
            LOG.info('Updated CronJob %s/%s', namespace, name)

    current = batch.read_namespaced_cron_job(name, namespace)
    active = len(current.status.active or []) if current.status else 0
    last_schedule = current.status.last_schedule_time if current.status else None
    last_success = current.status.last_successful_time if current.status else None
    # CronJob status reflects successful Job execution, not durable backup storage.
    extra = {'activeJobs': active}
    if last_schedule:
        extra['lastScheduleTime'] = last_schedule.isoformat()
    if last_success:
        extra['lastSuccessfulBackupTime'] = last_success.isoformat()
    reason = 'Suspended' if suspend else 'Reconciled'
    patch_status(custom, namespace, name, generation, True, reason,
                 f'CronJob reconciled; retention={retention} is design-only', **extra)


@kopf.on.startup()
def startup(**_):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
def on_change(spec, name, namespace, uid, meta, **_):
    reconcile(spec, name, namespace, uid, meta.get('generation', 1))


@kopf.timer(GROUP, VERSION, PLURAL, interval=30, sharp=False)
def periodic(spec, name, namespace, uid, meta, **_):
    try:
        reconcile(spec, name, namespace, uid, meta.get('generation', 1))
    except ApiException as exc:
        LOG.exception('Transient Kubernetes API failure for %s/%s: %s', namespace, name, exc)
        raise kopf.TemporaryError('Kubernetes API unavailable', delay=30)
