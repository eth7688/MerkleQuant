import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Home from '@/pages/Home'
import Funding from '@/pages/Funding'
import Opportunities from '@/pages/Opportunities'
import NotFound from '@/pages/NotFound'

const routerBase = import.meta.env.BASE_URL.replace(/\/$/, '') || '/'

export default function App() {
  return (
    <BrowserRouter basename={routerBase} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/funding" element={<Funding />} />
        <Route path="/opportunities" element={<Opportunities />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </BrowserRouter>
  )
}
