from calendar import monthrange
from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist

from dcim.models import Device, DeviceType
from extras.scripts import IntegerVar, Script

try:
    from netbox_ndx.models import NDXImportRecord
except ImportError:
    NDXImportRecord = None

MILESTONES = [
    ("eol_announced", "EOL Announced"),
    ("eos_date", "End of Sale"),
    ("eosw_date", "End of SW Support"),
    ("eosec_date", "End of Security Support"),
    ("last_support_date", "Last Day of Support (End of Life)"),
]


def parse_lifecycle_date(value):
    """NDX lifecycle fields are free-text and may be YYYY-MM-DD, YYYY-MM, or YYYY."""
    if not value:
        return None
    parts = value.strip().split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            return date(year, month, monthrange(year, month)[1])
        if len(parts) == 1 and parts[0].isdigit():
            return date(int(parts[0]), 12, 31)
    except (ValueError, IndexError):
        return None
    return None


def add_months(start, months):
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


class HardwareLifecycleHorizon(Script):
    class Meta:
        name = "Hardware Lifecycle Horizon"
        description = (
            "Devices reaching an NDX-published lifecycle milestone (EOL announced, "
            "end of sale, end of SW/security support, last day of support) within N "
            "months from today. Includes anything already overdue. Queries the "
            "netbox_ndx plugin's data live — nothing is duplicated or cached."
        )

    months = IntegerVar(
        label="Horizon (months)",
        description=(
            "Show devices reaching a lifecycle milestone within this many months "
            "from today. Devices already past a milestone are always included."
        ),
        default=12,
        min_value=0,
    )

    def run(self, data, commit):
        if NDXImportRecord is None:
            self.log_failure("The netbox_ndx plugin is not installed on this instance.")
            return "netbox_ndx plugin not available."

        months = data["months"]
        today = date.today()
        horizon = add_months(today, months)

        devicetype_ct = ContentType.objects.get_for_model(DeviceType)
        records = NDXImportRecord.objects.filter(content_type=devicetype_ct)

        enrichment_by_devicetype_id = {}
        for record in records:
            try:
                enrichment_by_devicetype_id[record.object_id] = record.enrichment
            except ObjectDoesNotExist:
                continue

        devices = (
            Device.objects.select_related("device_type__manufacturer", "site")
            .order_by("site__name", "name")
        )

        hits = {key: [] for key, _ in MILESTONES}
        devicetype_ids_seen = set()
        devicetype_ids_linked = set()
        devicetype_ids_with_dates = set()

        for device in devices:
            dt_id = device.device_type_id
            devicetype_ids_seen.add(dt_id)
            enrichment = enrichment_by_devicetype_id.get(dt_id)
            if enrichment is None:
                continue
            devicetype_ids_linked.add(dt_id)
            for field, label in MILESTONES:
                parsed = parse_lifecycle_date(getattr(enrichment, field, ""))
                if parsed is None:
                    continue
                devicetype_ids_with_dates.add(dt_id)
                if parsed <= horizon:
                    status = "OVERDUE" if parsed < today else "upcoming"
                    hits[field].append((device, parsed, status))

        total_devices = devices.count()
        lines = [
            "Hardware Lifecycle Horizon Report",
            f"Generated {today.isoformat()} | Horizon: next {months} month(s), "
            f"through {horizon.isoformat()}",
            "",
            f"Devices scanned: {total_devices}",
            f"Device types linked to NDX catalog: {len(devicetype_ids_linked)} / "
            f"{len(devicetype_ids_seen)}",
            f"Device types with at least one published lifecycle date: "
            f"{len(devicetype_ids_with_dates)}",
            "",
        ]

        any_hits = False
        for field, label in MILESTONES:
            entries = hits[field]
            if not entries:
                continue
            any_hits = True
            lines.append(f"=== {label} ({len(entries)} device(s)) ===")
            for device, parsed, status in sorted(entries, key=lambda item: item[1]):
                model = f"{device.device_type.manufacturer.name} {device.device_type.model}"
                lines.append(
                    f"  {device.name:<20} {device.site.name:<14} {model:<35} "
                    f"{parsed.isoformat():<12} {status}"
                )
                message = (
                    f"{device.name} ({device.site.name}): {label} was "
                    f"{parsed.isoformat()} — already overdue"
                    if status == "OVERDUE"
                    else
                    f"{device.name} ({device.site.name}): {label} due "
                    f"{parsed.isoformat()} — within {months} month(s)"
                )
                if status == "OVERDUE":
                    self.log_failure(message)
                else:
                    self.log_warning(message)
            lines.append("")

        if not any_hits:
            self.log_success(
                f"No devices reach any lifecycle milestone within the next "
                f"{months} month(s)."
            )
            lines.append(
                f"No devices reach any lifecycle milestone within the next "
                f"{months} month(s)."
            )

        return "\n".join(lines)
