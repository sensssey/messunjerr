import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Cookies from "js-cookie";
import { apiLogin } from "../../services/api";

const Login = () => {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    try {
      const token = await apiLogin(username, password); 
      Cookies.set("token", token, { expires: 7 });
      navigate("/posts"); 
    } catch (err: any) {
      setError(err.message || "Login failed");
    }
  };

  return (
    <div className="w-screen h-screen flex flex-col items-center justify-center">
      <div className="w-92 h-60 border flex flex-col justify-between p-4 rounded-lg shadow">
        <div className="mb-5">
          <h2 className="text-center text-xl">Sign In</h2>
          {error && <p style={{ color: "red" }}>{error}</p>}
        </div>
        <form onSubmit={handleSubmit} className="flex flex-col gap-1">
          <input
            type="text"
            className="border rounded-xs p-1 shadow-xs"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="Username"
            required
          />
          <input
            type="password"
            className="border rounded-xs p-1 shadow-xs"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
            required
          />
          <button className="border mt-1 p-1 rounded-xs shadow-xs" type="submit">Login</button>
        </form>
        <div className="text-center">
          <Link to="/register">Dont have an account? <u>Register here</u></Link>
        </div>
      </div>
    </div>
  );
};

export default Login;