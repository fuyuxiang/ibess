import { lazy, Suspense } from 'react'
import { Spin, Tabs } from 'antd'
import { Navigate, useSearchParams } from 'react-router-dom'
import { PageHeading } from './Supervision'
const RoomSetup = lazy(() => import('./RoomSetup'))
const Registry = lazy(() => import('./Registry'))
export default function SiteConfiguration() {
  const [params,setParams] = useSearchParams()
  if(params.get('session_id'))return <Navigate replace to={'/sessions/'+encodeURIComponent(params.get('session_id')!)+'?prepare='+(params.get('setup')==='people'?'people':'1')}/>
  if(params.get('setup')==='people')return <Navigate replace to="/sessions"/>
  return <div className="sv-page"><PageHeading title="现场配置" description="配置评标室机位与区域，供场次复用。"/>
    <Tabs activeKey={params.get('tab')==='registry'?'registry':'sessions'} onChange={value=>setParams(previous=>{const next=new URLSearchParams(previous);next.set('tab',value);return next})} items={[
      {key:'sessions',label:'现场资源',children:<Suspense fallback={<Spin/>}><RoomSetup/></Suspense>},
      {key:'registry',label:'项目与场地',children:<Suspense fallback={<Spin/>}><Registry scope="site" embedded/></Suspense>},
    ]}/>
  </div>
}
