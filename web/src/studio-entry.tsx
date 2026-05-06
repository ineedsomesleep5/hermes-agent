import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import StudioPage from "./pages/StudioPage";

createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <StudioPage />
  </BrowserRouter>,
);
