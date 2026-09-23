import { ShieldAlert } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { Button } from '../components/ui/Button'
import { Card } from '../components/ui/Card'

export default function AccessDeniedPage() {
  const navigate = useNavigate()

  return (
    <Card>
      <div className="state-panel">
        <div className="state-icon is-error">
          <ShieldAlert size={24} />
        </div>
        <div className="state-title">Access denied</div>
        <div className="state-description">
          Your account role does not have permission to view this page. Contact an administrator if you believe
          this is a mistake.
        </div>
        <Button variant="secondary" onClick={() => navigate('/')}>
          Back to Home
        </Button>
      </div>
    </Card>
  )
}
