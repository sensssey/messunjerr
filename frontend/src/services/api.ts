import axios from "axios";
import Cookies from "js-cookie";

// Создаем экземпляр axios для API-запросов
export const apiClient = axios.create({
  baseURL: process.env.REACT_APP_API_URL,
});

// Добавляем токен авторизации к каждому запросу
apiClient.interceptors.request.use((config) => {
  const token = Cookies.get("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
}, (error) => {
  return Promise.reject(error);
});

// === Аутентификация ===
export const apiLogin = async (username: string, password: string): Promise<string> => {
  try {
    const formData = new FormData();
    formData.append("username", username);
    formData.append("password", password);

    const response = await apiClient.post("/auth/token", formData, {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    });

    const { access_token } = response.data;
    if (!access_token) {
      throw new Error("No access token received");
    }
    return access_token;
  } catch (error: any) {
    throw new Error(JSON.stringify(error.response?.data?.detail) || "Login failed");
  }
};

export const apiRegister = async (username: string, password: string): Promise<{token: string}> => {
  try {
    const response = await apiClient.post("/auth/register", {
      username,
      password,
    });
    if (!response.data.access_token) {
      throw new Error("No access token received");
    }
    
    return {
      token: response.data.access_token
    };
  } catch (error: any) {
    if (error.response?.data?.detail) {
      if (typeof error.response.data.detail === 'string') {
        throw new Error(error.response.data.detail);
      }
      const errors = error.response.data.detail;
      for (const err of errors) {
        if (err.loc && err.type === "value_error") {
          throw new Error("Validation error: " + err.msg);
        }
      }
    }
    throw new Error(error.message || "Registration failed");
  }
};

export interface UserData {
  user_id: number;
  username: string;
  avatar?: string;
}

export const fetch_information = async (): Promise<UserData> => {
  try{
      const response = await apiClient.get("auth/users/me")
      return response.data;
  }
  catch(error){
    throw new Error("Failed to fetch userdata");
  }
}


// === Посты ===
export const fetchPosts = async () => apiClient.get("/posts/");

interface CreatePostData {
  title: string;
  content: string;
}

export const createPost = async (data: CreatePostData) => {
  try {
    const response = await apiClient.post("/posts/", data);
    return response.data;
  } catch (error) {
    console.error("Ошибка при создании поста:", error);
    throw error;
  }
};

export const updatePost = async (postId: number, data: { title: string; content: string }) => {
  try {
    const response = await apiClient.put(`/posts/${postId}`, data);
    return response.data;
  } catch (error) {
    console.error("Error updating post:", error);
    throw error;
  }
};

export const deletePost = async (postId: number) => {
  try {
    await apiClient.delete(`/posts/${postId}`);
  } catch (error) {
    console.error("Error deleting post:", error);
    throw error;
  }
};

// === Аватар ===
export const getAvatar = async (): Promise<Blob | null> => {
  try {
    const response = await apiClient.get("/users/avatar", {
      responseType: "blob",
    });

    if (response.data.size === 0) {
      throw new Error("Server returned empty avatar");
    }

    return response.data;
  } catch (err) {
    console.error("Failed to fetch avatar", err);
    return null;
  }
};

export const uploadAvatar = async (file: File): Promise<boolean> => {
  try {
    const formData = new FormData();
    formData.append("file", file);

    await apiClient.post("/users/avatar", formData, {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    });

    return true;
  } catch (err) {
    console.error("Failed to upload avatar", err);
    return false;
  }
};

export const deleteAvatar = async () => {
  try {
    await apiClient.delete('/users/avatar');
  } catch (error) {
    console.error("Error deleting post:", error);
    throw error;
  }
}

