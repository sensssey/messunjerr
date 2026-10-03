import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { getAvatar, uploadAvatar, deleteAvatar, fetch_information, UserData} from '../../services/api';
import LogoutButton from '../../components/auth/logoutButton';

const ProfilePage = () => {
    const [avatarUrl, setAvatarUrl] = useState<string | null>(null);
    const [loading, setLoading] = useState({
      avatar: true,
      upload: false,
      delete: false,
    });
    const [error, setError] = useState<string | null>(null);
    const [userData, setUserData] = useState<UserData | null>(null);
    const [userError, setUserError] = useState<string | null>(null);
  
    // Загрузка данных пользователя
    useEffect(() => {
      const fetchUserData = async () => {
        try {
          setUserError(null);
          const data = await fetch_information();
          setUserData(data);
        } catch (err: any) {
          setUserError(err.message || "Failed to load user data");
        }
      };
  
      fetchUserData();
    }, []);
  
    // Загрузка аватара
    useEffect(() => {
      const loadAvatar = async () => {
        try {
          setLoading(prev => ({ ...prev, avatar: true }));
          setError(null);
  
          const avatarBlob = await getAvatar();
          if (avatarBlob) {
            const url = URL.createObjectURL(avatarBlob);
            setAvatarUrl(url);
          }
        } catch (err) {
          console.error('Avatar load error:', err);
          setError('Failed to load avatar');
        } finally {
          setLoading(prev => ({ ...prev, avatar: false }));
        }
      };
  
      loadAvatar();
  
      return () => {
        if (avatarUrl) URL.revokeObjectURL(avatarUrl);
      };
    }, []);

  // Загрузка нового аватара
  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    try {
      setLoading((prev) => ({ ...prev, upload: true }));
      setError(null);

      const success = await uploadAvatar(file);
      if (!success) throw new Error('Upload failed');

      const newAvatar = await getAvatar();
      if (newAvatar) {
        if (avatarUrl) URL.revokeObjectURL(avatarUrl);
        setAvatarUrl(URL.createObjectURL(newAvatar));
      }
    } catch (err) {
      console.error('Upload error:', err);
      setError('Failed to upload avatar');
    } finally {
      setLoading((prev) => ({ ...prev, upload: false }));
    }
  };

  // Удаление аватара
  const handleDelete = async () => {
    try {
      setLoading((prev) => ({ ...prev, delete: true }));
      setError(null);

      await deleteAvatar();
      if (avatarUrl) URL.revokeObjectURL(avatarUrl);
      setAvatarUrl(null);
    } catch (err) {
      console.error('Delete error:', err);
      setError('Failed to delete avatar');
    } finally {
      setLoading((prev) => ({ ...prev, delete: false }));
    }
  };

  return (
    <div>
        <div className="flex justify-between items-center border-b border-black pb-2.5 px-3 h-18">
            <Link to="/posts" className="flex items-center space-x-2 text-white px-4 py-2 mt-2 rounded
                 bg-gray-800 hover:bg-gray-600 transition duration-300">
                <span>Back to posts</span>
            </Link>
            <div className="flex items-center space-x-4">
                <LogoutButton/>
            </div>
        </div>
      {loading.avatar || !userData ? (
        <div className="flex items-center justify-center min-h-[calc(100vh-64px)]">
          <div className="text-center">
            {loading.avatar ? 'Loading profile...' : 'No user data available'}
          </div>
        </div>
      ) : (
        <div className="flex items-center justify-center min-h-[calc(100vh-64px)] p-5">
          <div className="text-center w-full max-w-md">
            {/* Блок аватара */}
            {avatarUrl ? (
              <img
                src={avatarUrl}
                alt="User avatar"
                className="w-56 h-56 rounded-lg object-cover border-2 mb-5 mx-auto "
                onError={() => setError('Failed to display avatar')}
              />
            ) : (
              <img
                src="https://static.thenounproject.com/png/1476975-200.png"
                alt="Default avatar"
                className="w-56 h-56 rounded-lg object-cover border-2 mb-5 mx-auto"
              />
            )}

            {/* Кнопки управления аватаром */}
            <div className="flex justify-center gap-3 mb-8 mt-4">
              <label className="flex items-center justify-center space-x-2 text-white 
                px-4 py-2 rounded bg-gray-800 hover:bg-gray-600 transition duration-300 cursor-pointer
                min-w-[140px] h-10">
                {loading.upload ? 'Uploading...' : 'Change Avatar'}
                <input
                  type="file"
                  accept="image/jpeg,image/png"
                  onChange={handleUpload}
                  disabled={loading.upload}
                  className="hidden"
                />
              </label>
              {avatarUrl && (
                <button
                  onClick={handleDelete}
                  disabled={loading.delete}
                  className="flex items-center justify-center space-x-2 text-white
                    px-4 py-2 rounded bg-gray-800 hover:bg-gray-600 transition duration-300
                    min-w-[140px] h-10"
                >
                  {loading.delete ? 'Deleting...' : 'Delete Avatar'}
                </button>
              )}
            </div>
            <h2 className="text-xl font-semibold mb-2">{userData.username}</h2>
            {userError && <p className="text-red-500">{userError}</p>}
            {error && <p className="text-red-500">{error}</p>}
          </div>
        </div>
      )}
    </div>
  );
};

export default ProfilePage;