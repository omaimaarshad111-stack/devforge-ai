import axios from "axios";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "",
  timeout: 15000,
});

api.getProjectFile = (projectId, filePath, version) =>
  api.get(
    `/api/projects/${projectId}/files/${filePath
      .split("/")
      .map(encodeURIComponent)
      .join("/")}`,
    { params: version ? { version } : undefined },
  );

api.interceptors.request.use((config) => {
  const token = sessionStorage.getItem("devforge-api-token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

export function getErrorMessage(error) {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 401) {
      return "API access token is missing or invalid. Add it using the key icon in the header.";
    }
    if (error.response?.status === 403) {
      return "The API rejected this remote connection. Configure API_ACCESS_TOKEN on the backend.";
    }
    if (error.response?.data?.detail) {
      const { detail } = error.response.data;
      if (Array.isArray(detail)) {
        return detail
          .map((item) => `${item.loc?.slice(1).join(".") || "request"}: ${item.msg}`)
          .join(" · ");
      }
      return detail;
    }
    if (error.code === "ECONNABORTED") {
      return "The API request timed out. Check that the backend is running.";
    }
    if (!error.response) {
      return "Could not reach the API. Start the FastAPI backend and try again.";
    }
  }
  return error?.message || "Something went wrong while communicating with the API.";
}

export default api;
