// src/components/posts/PostList.tsx
import React from "react";
import { PencilIcon, TrashIcon } from "@heroicons/react/24/solid";
import { Post } from "../../types/posts";


interface PostListProps {
  posts: Post[];
  onDeletePost: (postId: number) => void;
  setPosts: React.Dispatch<React.SetStateAction<Post[]>>;
  setIsEditModalOpen: React.Dispatch<React.SetStateAction<boolean>>;
  setEditingPost: React.Dispatch<React.SetStateAction<Post | null>>;
}

const PostList = ({ posts, onDeletePost, setPosts, setIsEditModalOpen, setEditingPost }: PostListProps) => {
  const handleEditClick = (post: Post) => {
    setEditingPost(post);
    setIsEditModalOpen(true);
  };

  return (
    <div className="flex flex-col items-center space-y-4">
      {posts.length === 0 ? (
        <p className="text-gray-500 text-center">No posts available yet.</p>
      ) : (
        posts.map((post) => (
          <div
            key={post.id}
            className="w-full max-w-md bg-white border border-gray-200 rounded-lg shadow p-4 relative"
          >
            <div className="absolute top-2 right-2 flex space-x-2">
              <button onClick={() => handleEditClick(post)}>
                <PencilIcon className="h-5 w-5 text-slate-800 hover:text-slate-500" />
              </button>
              <button onClick={() => onDeletePost(Number(post.id))}>
                <TrashIcon className="h-5 w-5 text-slate-800 hover:text-slate-500" />
              </button>
            </div>

            <h3 className="text-xl font-bold text-gray-800 mb-2">{post.title}</h3>
            <p className="text-gray-700">{post.content}</p>
            <small className="block mt-2 text-gray-500">{new Date(post.created_at).toLocaleString()}</small>
          </div>
        ))
      )}
    </div>
  );
};

export default PostList;