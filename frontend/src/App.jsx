import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import AnalyticsPage from './pages/AnalyticsPage'
import ExcelFilesPage from './pages/ExcelFilesPage'
import ExpenseReportMonthPage from './pages/ExpenseReportMonthPage'
import ExpenseReportsPage from './pages/ExpenseReportsPage'
import HomePage from './pages/HomePage'
import PolicyMasterPage from './pages/PolicyMasterPage'
import SettingsPage from './pages/SettingsPage'
import TripReportMonthPage from './pages/TripReportMonthPage'
import TripReportsPage from './pages/TripReportsPage'
import UploadPage from './pages/UploadPage'
import UsersRolesPage from './pages/UsersRolesPage'

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/expense-reports" element={<ExpenseReportsPage />} />
        <Route path="/expense-reports/:month" element={<ExpenseReportMonthPage />} />
        <Route path="/trip-reports" element={<TripReportsPage />} />
        <Route path="/trip-reports/:month" element={<TripReportMonthPage />} />
        <Route path="/policy-master" element={<PolicyMasterPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/excel-files" element={<ExcelFilesPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/users-roles" element={<UsersRolesPage />} />
      </Route>
    </Routes>
  )
}
