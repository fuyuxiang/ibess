import { querySupervision, type SupervisionQuery } from '../../api/supervision';
import { useRefreshingQuery } from './useRefreshingQuery';

export function useSupervision(params: SupervisionQuery, enabled = true) {
  const key = JSON.stringify(params);
  return useRefreshingQuery(key, signal => querySupervision(JSON.parse(key) as SupervisionQuery,signal),enabled);
}
