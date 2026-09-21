import {
  BarChart3,
  FileSpreadsheet,
  FileText,
  Home,
  Map,
  Settings,
  ShieldCheck,
  UploadCloud,
  Users,
} from 'lucide-react'

export const PRIMARY_NAV = [
  { to: '/', label: 'Home', icon: Home, end: true },
  { to: '/upload', label: 'Upload Files', icon: UploadCloud },
  { to: '/expense-reports', label: 'Expense Reports', icon: FileText },
  { to: '/trip-reports', label: 'Trip Reports', icon: Map },
  { to: '/policy-master', label: 'Policy Master', icon: ShieldCheck },
  { to: '/analytics', label: 'Analytics', icon: BarChart3 },
  { to: '/excel-files', label: 'Excel Files', icon: FileSpreadsheet },
]

export const ADMIN_NAV = [
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/users-roles', label: 'Users & Roles', icon: Users },
]

export const ALL_NAV = [...PRIMARY_NAV, ...ADMIN_NAV]
