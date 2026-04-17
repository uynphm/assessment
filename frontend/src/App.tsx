import { BrowserRouter, Routes, Route } from "react-router-dom";
import PoliciesTable from "./components/PoliciesTable";
import PolicyView from "./components/PolicyView";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<PoliciesTable />} />
        <Route path="/policy/:id" element={<PolicyView />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
