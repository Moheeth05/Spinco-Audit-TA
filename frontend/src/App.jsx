import { Route, Routes } from 'react-router-dom'
import { ProtectedRoute } from './components/auth/ProtectedRoute'
import { AppShell } from './components/layout/AppShell'
import AccessDeniedPage from './pages/AccessDeniedPage'
import AnalyticsPage from './pages/AnalyticsPage'
import AuditLogsPage from './pages/AuditLogsPage'
import DocumentViewerPage from './pages/DocumentViewerPage'
import ExcelFilesPage from './pages/ExcelFilesPage'
import ExpenseReportMonthPage from './pages/ExpenseReportMonthPage'
import ExpenseReportsPage from './pages/ExpenseReportsPage'
import HomePage from './pages/HomePage'
import LoginPage from './pages/LoginPage'
import PolicyMasterPage from './pages/PolicyMasterPage'
import SettingsPage from './pages/SettingsPage'
import TripReportMonthPage from './pages/TripReportMonthPage'
import TripReportsPage from './pages/TripReportsPage'
import UploadPage from './pages/UploadPage'
import UsersRolesPage from './pages/UsersRolesPage'

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/upload" element={<UploadPage />} />
          <Route path="/expense-reports" element={<ExpenseReportsPage />} />
          <Route path="/expense-reports/:month" element={<ExpenseReportMonthPage />} />
          <Route path="/documents/:documentId" element={<DocumentViewerPage />} />
          <Route path="/trip-reports" element={<TripReportsPage />} />
          <Route path="/trip-reports/:month" element={<TripReportMonthPage />} />
          <Route path="/policy-master" element={<PolicyMasterPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/excel-files" element={<ExcelFilesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/access-denied" element={<AccessDeniedPage />} />

          <Route element={<ProtectedRoute adminOnly />}>
            <Route path="/users-roles" element={<UsersRolesPage />} />
            <Route path="/audit-logs" element={<AuditLogsPage />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  )
}
