import {
  BarChart3,
  ClipboardList,
  FileSpreadsheet,
  FileText,
  Home,
  Map,
  Settings,
  ShieldCheck,
  UploadCloud,
  Users,
} from 'lucide-react'

/** `hideFor`: roles that should not see this item in the sidebar (UX only -
 * the backend enforces the actual permission independently either way). */
export const PRIMARY_NAV = [
  { to: '/', label: 'Home', icon: Home, end: true },
  { to: '/upload', label: 'Upload Files', icon: UploadCloud, hideFor: ['VIEWER'] },
  { to: '/expense-reports', label: 'Expense Reports', icon: FileText },
  { to: '/trip-reports', label: 'Trip Reports', icon: Map },
  { to: '/policy-master', label: 'Policy Master', icon: ShieldCheck },
  { to: '/analytics', label: 'Analytics', icon: BarChart3 },
  { to: '/excel-files', label: 'Excel Files', icon: FileSpreadsheet },
]

export const SETTINGS_NAV = [{ to: '/settings', label: 'Settings', icon: Settings }]

export const ADMIN_NAV = [
  { to: '/users-roles', label: 'Users & Roles', icon: Users },
  { to: '/audit-logs', label: 'Audit Logs', icon: ClipboardList },
]

export const ALL_NAV = [...PRIMARY_NAV, ...SETTINGS_NAV, ...ADMIN_NAV]
