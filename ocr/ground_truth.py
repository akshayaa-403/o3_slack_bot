from __future__ import annotations

GROUND_TRUTH: dict[str, list[str]] = {
    "adam_login_mfa_error.png": [
        "ADAM Portal",
        "Unable to sign in",
        "Error: ADAM portal shows blank page after MFA",
        "User completed Microsoft Authenticator approval.",
        "Browser: Chrome",
        "Request: help me fix ADAM login issue",
        "Project IVY image-flow test screenshot",
    ],
    "vpn_connection_failed.png": [
        "GlobalProtect VPN",
        "Connection Failed",
        "Error: VPN connection failed",
        "The network connection is unreachable.",
        "Gateway: gpcloudservice.com",
        "Request: VPN not connecting",
        "Project IVY image-flow test screenshot",
    ],
    "pcq_access_denied.png": [
        "PCQ Access",
        "Access Denied",
        "Error: You do not have access to PCQ",
        "Contact IT support to request access.",
        "User needs PCQ application access.",
        "Request: I need access to PCQ",
        "Project IVY image-flow test screenshot",
    ],
    "outlook_mailbox_full.png": [
        "Microsoft Outlook",
        "Mailbox Full",
        "Error: Your mailbox is full",
        "You cannot send or receive email.",
        "Please reduce mailbox size.",
        "Request: Outlook mailbox full issue",
        "Project IVY image-flow test screenshot",
    ],
    "unmatched_hr_timesheet_error.png": [
        "TimeTrack Portal",
        "Timesheet submission failed",
        "Error: Timesheet period is locked for payroll processing",
        "Employee: Test User",
        "Week ending: 30 Jun 2026",
        "Request: help me submit my locked timesheet",
        "Project IVY unmatched image-flow test screenshot",
    ],
}


def reference_text(file_name: str) -> str:
    """Full reference transcription for a fixture, newline-joined."""

    return "\n".join(GROUND_TRUTH.get(file_name, []))
