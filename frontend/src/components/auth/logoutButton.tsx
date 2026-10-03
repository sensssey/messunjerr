import React from "react";
import { useNavigate } from "react-router-dom";
import Cookies from "js-cookie";

const LogoutButton = () => {
  const navigate = useNavigate();
  const handleLogout = () => {
    Cookies.remove("token"); 
    navigate("/"); 
  };

  return (
    <button
  onClick={handleLogout}
  className="flex items-center space-x-2 text-white px-4 py-2 rounded bg-gray-800 
   hover:bg-gray-600 transition duration-300 mt-2">
  <svg
    xmlns="http://www.w3.org/2000/svg"
    className="h-5 w-5"
    viewBox="0 0 20 20"
    fill="currentColor"
  >
    <path
      fillRule="evenodd"
      d="M3 3a1 1 0 00-1 1v12a1 1 0 102 0V4a1 1 0 00-1-1zm10.293 9.293a1 1 0 001.414 1.414l3-3a1 1 0 000-1.414l-3-3a1 1 0 10-1.414 1.414L14.586 9H7a1 1 0 100 2h7.586l-1.293 1.293z"
      clipRule="evenodd"
    />
  </svg>
  <span>Logout</span>
</button>
  );
};

export default LogoutButton;