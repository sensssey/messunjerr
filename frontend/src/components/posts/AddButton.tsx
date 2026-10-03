import React, { useState } from "react";
import Modal from "./Modal";
import { createPost } from "../../services/api";
import { Post } from "../../types/posts";

interface AddPostButtonProps {
  setPosts: React.Dispatch<React.SetStateAction<Post[]>>; 
}

const AddPostButton = ({ setPosts }: AddPostButtonProps) => {
  const [isModalOpen, setIsModalOpen] = useState(false); 
  const [title, setTitle] = useState(""); 
  const [content, setContent] = useState(""); 

  const handleOpenModal = () => {
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setTitle(""); 
    setContent("");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    try {
      const newPost = await createPost({ title, content });
      console.log("Post created successfully:", newPost);
      setPosts((prevPosts) => [newPost, ...prevPosts]);
      setTitle("");
      setContent("");
      handleCloseModal();
    } catch (error) {
      console.error("Error creating post:", error);
    }
  };

  return (
    <>
      <button
        onClick={handleOpenModal}
        className="fixed bottom-4 right-4 bg-gray-800 text-white p-4 rounded-full shadow-lg hover:bg-gray-600 transition duration-300 z-50"
      >
        +
      </button>
      <Modal isOpen={isModalOpen} onClose={handleCloseModal}>
        <h2 className="text-xl font-bold text-gray-800 mb-4">Create New Post</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block mb-1 text-sm font-medium text-gray-700">Title</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full border border-gray-300 rounded p-2 text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Enter post title"
              required
            />
          </div>
  
          <div>
            <label className="block mb-1 text-sm font-medium text-gray-700">Content</label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              className="w-full border border-gray-300 rounded p-2 text-sm placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Enter post content"
              rows={4}
              required
            />
          </div>
  
          <button
            type="submit"
            className="bg-gray-800 text-white py-2 rounded w-full hover:bg-gray-600 transition duration-300"
          >
            Create Post
          </button>
        </form>
      </Modal>
    </>
  );
};

export default AddPostButton;