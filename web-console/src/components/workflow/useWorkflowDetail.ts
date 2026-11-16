import { useSearchParams } from 'react-router-dom'
import { fetchEventDetail } from '../../api/client'
import { useRefreshingQuery } from './useRefreshingQuery'

export function useWorkflowDetail() {
  const [searchParams] = useSearchParams()
  const eventId = searchParams.get('event_id') || ''
  const query = useRefreshingQuery(eventId, signal => fetchEventDetail(eventId, signal), Boolean(eventId))
  return { ...query, detail: query.data, eventId }
}
